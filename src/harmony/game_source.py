"""Game source: Harmony as a proxy music source for a game engine.

A game (e.g. a Skyrim SKSE plugin) asks Harmony for "a track for the *combat*
music type", downloads it as plain PCM WAV and plays it with its own audio
engine. Harmony does the picking (profiles + shuffled "bags"), the sourcing
(Library, Qobuz, YouTube Music through the engine's providers) and the
transcoding (ffmpeg -> exact-length RIFF/WAVE, cached). The HTTP surface is
documented in ``docs/api/game-source.md``; this module is the whole
implementation, kept gi-free and independent of the HTTP framework:

* :func:`validate_profile` / :func:`validate_name` -- strict profile schema.
* :class:`TrackPool` -- candidate tracks for a (profile, type) + the bag.
* :class:`AudioCache` -- single-flight ffmpeg transcode + LRU WAV cache.
* :class:`GameSource` -- the request router the web server delegates to
  (:meth:`GameSource.handle`) returning a :class:`GameResponse`;
  :func:`send_response` writes one to a ``BaseHTTPRequestHandler``.

The engine is used through a small duck-typed surface (all on
``harmony.web.api.Engine``): ``_library_provider()``, ``_ensure_providers()``,
``_provider(service)``, ``_plock(service)``, ``_library_preferred()``,
``_match_library(items)``, ``_resolve_preferring_library(service, id)`` and
``_source_for(service, id)``. Tests substitute a fake with those methods.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import json
import logging
import os
import random
import re
import shutil
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from harmony.streamio import file_url_path, is_file_url

log = logging.getLogger(__name__)

# -- limits / constants -----------------------------------------------------------

SOURCES = ("library", "all")
SERVICES = ("local", "qobuz", "ytmusic")
#: Services usable with no account at all: YouTube Music's public catalogue
#: (ytmusicapi public search/playlists/albums + a yt-dlp resolve). Qobuz needs a login.
PUBLIC_SERVICES = ("ytmusic",)
#: Streaming tracks of a known length outside this range are not offered (a
#: 2-hour "sleep music" video or a 20 s clip is no use as game music, and would
#: be a huge WAV). Library tracks are never filtered.
STREAM_MIN_S = 30
STREAM_MAX_S = 15 * 60
BAD_TTL_S = 1800.0            # an unresolvable (region/age-blocked...) track is skipped this long
RATES = (44100, 48000)
BITS = (16, 24)
CHANNELS = (1, 2)
DEFAULT_FORMAT = {"rate": 48000, "bits": 16, "channels": 2}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TYPE_RE = NAME_RE
# Provider ids: Qobuz digits, YouTube 11-char, library sha-ish; a federated
# library id carries "@host:port" (IPv6 in brackets).
ID_RE = re.compile(r"^[A-Za-z0-9_.:@+=~\[\]-]{1,200}$")
SELECTOR_KINDS = ("genre", "playlist", "album", "artist", "query")

MAX_TYPES = 64
MAX_SELECTORS = 16
MAX_TEXT = 200
MAX_COUNT = 10
MAX_PROFILES = 64

POOL_TTL_S = 600.0            # candidate list refresh interval
GENRE_CAP = 1000              # library songs pulled for a genre selector
LIB_SEARCH_LIMIT = 50
STREAM_SEARCH_LIMIT = 25
ARTIST_TRACKS = 100

DEFAULT_CACHE_MB = 2048
MIN_CACHE_MB = 64
DEFAULT_WAIT_S = 30.0         # how long an audio request blocks while transcoding
MAX_WAIT_S = 120.0
RETRY_AFTER_S = 2
TRANSCODE_TIMEOUT_S = 900
MAX_AUDIO_S = 2 * 60 * 60     # longer sources are cut (keeps RIFF sizes < 4 GiB)
FAIL_TTL_S = 20.0             # a failed transcode is not retried for this long
HISTORY_MAX = 50
META_MAX = 5000

#: Loudness normalisation target (EBU R128, single pass ``loudnorm``).
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
_SILENCE = "silenceremove=start_periods=1:start_threshold=-50dB:start_duration=0.02"
_PIPELINE = "v1"              # bump to invalidate the WAV cache when the pipeline changes


class GameError(Exception):
    """A request-level error rendered as ``{"error", "code"}`` JSON."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# -- validation -------------------------------------------------------------------


def _bad(path: str, message: str) -> GameError:
    return GameError(400, "invalid_profile", f"{path}: {message}")


def validate_name(name: Any, what: str = "profile name") -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise GameError(400, "invalid_name",
                        f"{what} must be 1-64 characters of letters, digits, '_', '-' or '.' "
                        "(starting with a letter or digit)")
    return name


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad(path, "must be a non-empty string")
    value = value.strip()
    if len(value) > MAX_TEXT or any(ord(c) < 32 for c in value):
        raise _bad(path, f"must be at most {MAX_TEXT} printable characters")
    return value


def validate_ref(value: Any, path: str) -> dict[str, str]:
    """``{"service": "qobuz", "id": "123"}`` -> normalised copy."""
    if not isinstance(value, dict) or set(value) != {"service", "id"}:
        raise _bad(path, 'must be an object with exactly "service" and "id"')
    service, item_id = value["service"], value["id"]
    if service not in SERVICES:
        raise _bad(f"{path}.service", f"must be one of {', '.join(SERVICES)}")
    if isinstance(item_id, int) and not isinstance(item_id, bool):
        item_id = str(item_id)
    if not isinstance(item_id, str) or not ID_RE.match(item_id):
        raise _bad(f"{path}.id", "must be a plain provider id (letters, digits, _ . : @ + = ~ -)")
    return {"service": service, "id": item_id}


def validate_selector(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) != 1:
        raise _bad(path, f"must be an object with exactly one of {', '.join(SELECTOR_KINDS)}")
    (kind, arg), = value.items()
    if kind in ("genre", "query"):
        return {kind: _text(arg, f"{path}.{kind}")}
    if kind in ("playlist", "album", "artist"):
        return {kind: validate_ref(arg, f"{path}.{kind}")}
    raise _bad(path, f"unknown selector {kind!r} (use {', '.join(SELECTOR_KINDS)})")


def validate_selectors(value: Any, path: str) -> dict[str, Any] | list[dict[str, Any]]:
    """One selector, or a non-empty list of them (their union)."""
    if isinstance(value, list):
        if not 1 <= len(value) <= MAX_SELECTORS:
            raise _bad(path, f"a selector list needs 1-{MAX_SELECTORS} entries")
        return [validate_selector(v, f"{path}[{i}]") for i, v in enumerate(value)]
    return validate_selector(value, path)


def _int_choice(value: Any, choices: tuple[int, ...], path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in choices:
        raise _bad(path, f"must be one of {', '.join(map(str, choices))}")
    return value


def _flag(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _bad(path, "must be true or false")
    return value


def validate_profile(data: Any, name: str | None = None) -> dict[str, Any]:
    """Strictly validate a profile and return it with every default filled in.

    Unknown keys are rejected (a typo must not silently do nothing). ``name``
    (the URL's profile name) may be repeated in the body if it matches.
    """
    if not isinstance(data, dict):
        raise _bad("profile", "must be a JSON object")
    allowed = {"sources", "types", "format", "normalize", "trim_silence"}
    extra = set(data) - allowed - ({"name"} if name else set())
    if extra:
        raise _bad("profile", f"unknown key(s): {', '.join(sorted(map(str, extra)))}")
    if "name" in data and data["name"] != name:
        raise _bad("name", "does not match the profile name in the URL")
    sources = data.get("sources", "library")
    if sources not in SOURCES:
        raise _bad("sources", 'must be "library" or "all"')
    types = data.get("types")
    if not isinstance(types, dict) or not 1 <= len(types) <= MAX_TYPES:
        raise _bad("types", f"must be an object mapping 1-{MAX_TYPES} music types to selectors")
    out_types: dict[str, Any] = {}
    for tname, sel in types.items():
        if not isinstance(tname, str) or not TYPE_RE.match(tname):
            raise _bad(f"types.{tname}", "music type names are 1-64 letters, digits, '_', '-' or '.'")
        out_types[tname] = validate_selectors(sel, f"types.{tname}")
    fmt_in = data.get("format", {})
    if not isinstance(fmt_in, dict):
        raise _bad("format", "must be an object")
    extra = set(fmt_in) - set(DEFAULT_FORMAT)
    if extra:
        raise _bad("format", f"unknown key(s): {', '.join(sorted(map(str, extra)))}")
    fmt = {
        "rate": _int_choice(fmt_in.get("rate", 48000), RATES, "format.rate"),
        "bits": _int_choice(fmt_in.get("bits", 16), BITS, "format.bits"),
        "channels": _int_choice(fmt_in.get("channels", 2), CHANNELS, "format.channels"),
    }
    return {
        "sources": sources, "types": out_types, "format": fmt,
        "normalize": _flag(data.get("normalize", False), "normalize"),
        "trim_silence": _flag(data.get("trim_silence", False), "trim_silence"),
    }


def selectors_of(value: Any) -> list[dict[str, Any]]:
    return value if isinstance(value, list) else [value]


# -- WAV --------------------------------------------------------------------------


def wav_header(data_len: int, rate: int, bits: int, channels: int) -> bytes:
    """Canonical 44-byte RIFF/WAVE PCM header for ``data_len`` bytes of samples
    (WAVE_FORMAT_PCM, also for 24-bit -- no EXTENSIBLE, no LIST chunk)."""
    block = channels * bits // 8
    pad = data_len % 2  # RIFF chunks are word-aligned
    return (b"RIFF" + struct.pack("<I", 36 + data_len + pad) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block, block, bits)
            + b"data" + struct.pack("<I", data_len))


def wav_valid(path: Path) -> bool:
    """Whether ``path`` is a complete WAV written by us (header sizes match the file)."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(44)
    except OSError:
        return False
    if len(head) < 44 or head[:4] != b"RIFF" or head[8:16] != b"WAVEfmt " or head[36:40] != b"data":
        return False
    riff, = struct.unpack("<I", head[4:8])
    data, = struct.unpack("<I", head[40:44])
    return data > 0 and riff + 8 == size and 44 + data + data % 2 == size


# -- ffmpeg -----------------------------------------------------------------------


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def ffmpeg_input_args(url: str, headers: Mapping[str, str] | None = None) -> list[str]:
    """``-i`` arguments for an engine-resolved source: a library ``file://`` or
    an http(s) stream with its request headers. Nothing else is accepted, and
    ffmpeg's protocol whitelist stops a playlist/manifest pulling in other
    schemes."""
    if is_file_url(url):
        return ["-protocol_whitelist", "file", "-i", str(file_url_path(url))]
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        raise GameError(502, "bad_source", "the resolved source is not a file or an http(s) stream")
    args = ["-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-rw_timeout", "30000000"]
    ua, extra = None, []
    for key, value in (headers or {}).items():
        if any(c in f"{key}{value}" for c in "\r\n"):
            continue
        if key.lower() == "user-agent":
            ua = value
        else:
            extra.append(f"{key}: {value}")
    if ua:
        args += ["-user_agent", ua]
    if extra:
        args += ["-headers", "".join(f"{h}\r\n" for h in extra)]
    return args + ["-i", url]


def build_filter(rate: int, bits: int, channels: int, normalize: bool, trim: bool) -> str:
    chain: list[str] = []
    if trim:  # leading silence, then the same on the reversed signal for the tail
        chain += [_SILENCE, "areverse", _SILENCE, "areverse"]
    if normalize:
        chain.append(LOUDNORM)
    if channels == 1:
        chain.append("aformat=channel_layouts=mono")
    else:
        chain.append("aformat=channel_layouts=stereo")
    # The final stage owns the rate/sample-format conversion so 16-bit gets dither.
    chain.append(f"aresample=osr={rate}:osf={'s16' if bits == 16 else 's32'}:dither_method=triangular_hp")
    return ",".join(chain)


_URL_RE = re.compile(r"https?://\S+")


def _scrub(text: str) -> str:
    from harmony.config import redact_secrets

    return redact_secrets(_URL_RE.sub("<url>", text))


def transcode_to_wav(ffmpeg: str, in_args: list[str], dest: Path, *, rate: int, bits: int,
                     channels: int, normalize: bool, trim: bool) -> int:
    """Decode the input with ffmpeg into ``dest`` as a canonical WAV; returns the
    number of PCM bytes. ``dest`` is only meaningful on success (the caller
    renames it into place); raises :class:`GameError` otherwise."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *in_args,
           "-vn", "-sn", "-dn", "-map", "0:a:0", "-map_metadata", "-1",
           "-af", build_filter(rate, bits, channels, normalize, trim),
           "-t", str(MAX_AUDIO_S), "-f", "s16le" if bits == 16 else "s24le", "pipe:1"]
    block = channels * bits // 8
    with open(dest, "wb", buffering=0) as out:
        out.write(b"\0" * 44)  # header slot, filled in once the length is known
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, engine-resolved input only
                cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.PIPE,
                timeout=TRANSCODE_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired as exc:
            raise GameError(504, "transcode_timeout", "ffmpeg took too long") from exc
        if proc.returncode != 0:
            tail = _scrub(proc.stderr.decode("utf-8", "replace").strip()[-300:])
            raise GameError(502, "transcode_failed", f"ffmpeg failed (rc={proc.returncode}): {tail}")
        data_len = out.seek(0, os.SEEK_END) - 44
        data_len -= data_len % block  # whole frames only
        if data_len <= 0:
            raise GameError(502, "transcode_failed", "ffmpeg produced no audio")
        if data_len >= 0xFFFFFFFF - 64:
            raise GameError(502, "too_long", "the track is too long for a WAV file")
        out.truncate(44 + data_len)
        if data_len % 2:
            out.seek(0, os.SEEK_END)
            out.write(b"\0")
        out.seek(0)
        out.write(wav_header(data_len, rate, bits, channels))
        os.fsync(out.fileno())
    return data_len


# -- audio cache --------------------------------------------------------------------


@dataclass
class _Job:
    done: threading.Event = field(default_factory=threading.Event)
    error: GameError | None = None


class AudioCache:
    """Finished WAVs on disk (LRU by mtime, size-capped) + single-flight builds.

    ``get(key, build, wait_s)``: a hit returns the path at once; a miss starts
    ``build(tmp_path)`` on a worker thread (so a client giving up never kills a
    transcode) shared by every concurrent caller of the same key, and blocks up
    to ``wait_s`` for it. ``None`` means "still working" (the HTTP layer answers
    202 + Retry-After). Files appear atomically (temp + rename).
    """

    def __init__(self, directory: Path, cap_bytes: Callable[[], int]) -> None:
        self.dir = Path(directory)
        self._cap = cap_bytes
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._failed: dict[str, tuple[float, GameError]] = {}
        self.dir.mkdir(parents=True, exist_ok=True)
        self._sweep_parts()

    def path_for(self, key: str) -> Path:
        return self.dir / f"{key}.wav"

    def _sweep_parts(self) -> None:
        """Remove half-written temp files left by a crashed run."""
        cutoff = time.time() - 3600
        for p in self.dir.glob("*.part"):
            with contextlib.suppress(OSError):
                if p.stat().st_mtime < cutoff:
                    p.unlink()

    def stats(self) -> dict[str, int]:
        total, files = 0, 0
        for p in self.dir.glob("*.wav"):
            with contextlib.suppress(OSError):
                total += p.stat().st_size
                files += 1
        return {"bytes": total, "files": files, "cap": int(self._cap())}

    def _hit(self, key: str) -> Path | None:
        p = self.path_for(key)
        if not p.exists():
            return None
        if not wav_valid(p):
            log.warning("game-audio: discarding invalid cache file %s", p.name)
            with contextlib.suppress(OSError):
                p.unlink()
            return None
        with contextlib.suppress(OSError):
            os.utime(p)  # LRU touch
        return p

    def get(self, key: str, build: Callable[[Path], None], wait_s: float) -> Path | None:
        hit = self._hit(key)
        if hit is not None:
            return hit
        with self._lock:
            hit = self._hit(key)  # finished while we waited for the lock
            if hit is not None:
                return hit
            failed = self._failed.get(key)
            if failed and time.monotonic() - failed[0] < FAIL_TTL_S:
                raise failed[1]
            job = self._jobs.get(key)
            if job is None:
                job = self._jobs[key] = _Job()
                threading.Thread(target=self._run, args=(key, job, build), daemon=True,
                                 name=f"game-audio-{key[:8]}").start()
        job.done.wait(max(0.0, wait_s))
        if not job.done.is_set():
            return None
        if job.error is not None:
            raise job.error
        hit = self._hit(key)
        if hit is None:  # evicted between finishing and now: report as still working
            return None
        return hit

    def _run(self, key: str, job: _Job, build: Callable[[Path], None]) -> None:
        final = self.path_for(key)
        tmp = self.dir / f"{key}.{os.getpid()}.{threading.get_ident()}.part"
        try:
            build(tmp)
            os.replace(tmp, final)
            self._evict(keep=final)
        except GameError as exc:
            job.error = exc
        except Exception as exc:  # noqa: BLE001 - never lose the job
            log.warning("game-audio: build of %s failed: %s", key[:8], exc)
            job.error = GameError(502, "transcode_failed", _scrub(str(exc)) or "transcode failed")
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
            with self._lock:
                self._jobs.pop(key, None)
                if job.error is not None:
                    self._failed[key] = (time.monotonic(), job.error)
                else:
                    self._failed.pop(key, None)
            job.done.set()

    def _evict(self, keep: Path | None = None) -> None:
        cap = int(self._cap())
        files: list[tuple[float, int, Path]] = []
        total = 0
        for p in self.dir.glob("*.wav"):
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
            total += st.st_size
        for _mtime, size, p in sorted(files):
            if total <= cap:
                break
            if keep is not None and p == keep:
                continue
            with contextlib.suppress(OSError):
                p.unlink()  # an open serve keeps its fd; later requests re-transcode
                total -= size


# -- candidate pools + bag ------------------------------------------------------------


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _ref(t: Any) -> dict[str, Any] | None:
    """A ``Track`` (or track dict) as the light candidate record we keep."""
    if isinstance(t, dict):
        d = t
        service, item_id = str(d.get("service") or ""), str(d.get("id") or "")
        artist = d.get("artist") or ""
    else:
        service, item_id = t.service.value, str(t.id or "")
        artist = t.artist_name
        d = {"title": t.title, "album": t.album, "duration_s": t.duration_s,
             "quality": t.quality, "genre": t.genre, "isrc": t.isrc}
    if not service or not item_id:
        return None
    return {"service": service, "id": item_id, "title": d.get("title") or "", "artist": artist,
            "album": d.get("album"), "duration_s": d.get("duration_s"),
            "quality": d.get("quality") if isinstance(d.get("quality"), dict) else None,
            "genre": d.get("genre"), "isrc": d.get("isrc")}


class Bag:
    """A shuffled bag over a set of keys: every key once per cycle, no key
    twice in a row across a cycle boundary (when there is more than one).

    The upcoming order is materialised, so :meth:`take` with ``consume=False``
    (peek) returns exactly what a later consuming call will return."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()  # noqa: S311 - shuffling music, not crypto
        self.members: list[tuple[str, str]] = []
        self.queue: collections.deque[tuple[str, str]] = collections.deque()
        self.last: tuple[str, str] | None = None

    def sync(self, keys: list[tuple[str, str]]) -> None:
        """Adopt a (possibly changed) candidate set without restarting the cycle."""
        old = set(self.members)
        new = set(keys)
        self.members = list(dict.fromkeys(keys))
        if not old:
            self.queue = collections.deque(self.rng.sample(self.members, len(self.members)))
            return
        kept = [k for k in self.queue if k in new]
        for k in self.members:
            if k not in old:  # newcomers go to a random spot in what's left
                kept.insert(self.rng.randint(0, len(kept)), k)
        self.queue = collections.deque(kept)

    def _fill(self, n: int) -> None:
        while len(self.queue) < n and self.members:
            cycle = self.rng.sample(self.members, len(self.members))
            prev = self.queue[-1] if self.queue else self.last
            if len(cycle) > 1 and cycle[0] == prev:
                j = self.rng.randrange(1, len(cycle))
                cycle[0], cycle[j] = cycle[j], cycle[0]
            self.queue.extend(cycle)

    def take(self, n: int, *, consume: bool) -> list[tuple[str, str]]:
        self._fill(n)
        items = list(self.queue)[:n]
        if consume:
            for _ in items:
                self.queue.popleft()
            if items:
                self.last = items[-1]
        return items


class TrackPool:
    """Candidate tracks for one (profile, type, sources) + its bag."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fingerprint = ""
        self.built = 0.0
        self.refs: dict[tuple[str, str], dict[str, Any]] = {}
        self.bag = Bag()


# -- the router ---------------------------------------------------------------------


@dataclass
class GameResponse:
    status: int = 200
    body: Any = None                      # JSON-serialisable
    headers: dict[str, str] = field(default_factory=dict)
    file: Path | None = None              # served (a byte range of) as audio/wav
    start: int = 0
    length: int = 0


def _json(body: Any, status: int = 200, **headers: str) -> GameResponse:
    return GameResponse(status=status, body=body, headers=dict(headers))


def _error(exc: GameError) -> GameResponse:
    return _json({"error": exc.message, "code": exc.code}, exc.status)


def parse_range(header: str | None, size: int) -> tuple[int, int] | str | None:
    """``(start, end)`` inclusive for a satisfiable single range, ``"bad"`` when
    unsatisfiable (416), ``None`` to ignore (no/invalid/multi range -> full body)."""
    if not header:
        return None
    m = re.match(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", header)
    if not m or not (m.group(1) or m.group(2)):
        return None
    first, last = m.group(1), m.group(2)
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    else:
        n = int(last)
        if n == 0:
            return "bad"
        start, end = max(0, size - n), size - 1
    if start >= size or start > end:
        return "bad"
    return start, end


class GameSource:
    """Profiles, bags, now-playing and the WAV cache behind ``/api/game/*``."""

    def __init__(self, engine: Any, *, cache_dir: Path | None = None,
                 ffmpeg: Callable[[], str | None] = find_ffmpeg,
                 settings: Callable[[], Any] | None = None,
                 wait_s: float = DEFAULT_WAIT_S) -> None:
        self.engine = engine
        self._ffmpeg = ffmpeg
        self._settings = settings or _load_settings
        self.wait_s = wait_s
        if cache_dir is None:
            from harmony.config import cache_dir as _cache_dir

            cache_dir = _cache_dir() / "game-audio"
        self.cache = AudioCache(cache_dir, self._cap_bytes)
        self._profiles_lock = threading.Lock()
        self._pools: dict[tuple[str, str, str], TrackPool] = {}
        self._pools_lock = threading.Lock()
        self._meta: collections.OrderedDict[tuple[str, str], dict[str, Any]] = collections.OrderedDict()
        self._meta_lock = threading.Lock()
        self._bad: dict[tuple[str, str], float] = {}
        self._now: dict[str, Any] | None = None
        self._history: collections.deque[dict[str, Any]] = collections.deque(maxlen=HISTORY_MAX)

    # -- settings ---------------------------------------------------------------

    def _cap_bytes(self) -> int:
        try:
            mb = int(getattr(self._settings(), "game_cache_max_mb", DEFAULT_CACHE_MB))
        except (TypeError, ValueError):
            mb = DEFAULT_CACHE_MB
        return max(MIN_CACHE_MB, mb) * 1024 * 1024

    def enabled(self) -> bool:
        return bool(getattr(self._settings(), "game_source_enabled", True))

    def _stored(self) -> dict[str, Any]:
        raw = getattr(self._settings(), "game_profiles", None)
        return raw if isinstance(raw, dict) else {}

    def get_profile(self, name: str) -> dict[str, Any]:
        raw = self._stored().get(name)
        if raw is None:
            raise GameError(404, "unknown_profile", f"no game profile named {name!r}")
        try:
            return validate_profile(raw)
        except GameError as exc:
            raise GameError(409, "invalid_profile", f"stored profile {name!r} is invalid ({exc.message}); "
                            "PUT it again") from exc

    def list_profiles(self) -> list[dict[str, Any]]:
        out = []
        for name in sorted(self._stored()):
            try:
                out.append({"name": name, **validate_profile(self._stored()[name])})
            except GameError as exc:
                log.warning("game profile %r is invalid: %s", name, exc.message)
        return out

    def put_profile(self, name: str, body: Any) -> dict[str, Any]:
        validate_name(name)
        profile = validate_profile(body, name)
        from harmony.config import Settings

        with self._profiles_lock:
            s = Settings.load()
            profiles = dict(s.game_profiles) if isinstance(s.game_profiles, dict) else {}
            if name not in profiles and len(profiles) >= MAX_PROFILES:
                raise GameError(400, "too_many_profiles", f"at most {MAX_PROFILES} profiles")
            profiles[name] = profile
            s.game_profiles = profiles  # assignment, so save() merges it
            s.save()
        self._drop_pools(name)
        return {"name": name, **profile}

    def delete_profile(self, name: str) -> None:
        validate_name(name)
        from harmony.config import Settings

        with self._profiles_lock:
            s = Settings.load()
            profiles = dict(s.game_profiles) if isinstance(s.game_profiles, dict) else {}
            if name not in profiles:
                raise GameError(404, "unknown_profile", f"no game profile named {name!r}")
            del profiles[name]
            s.game_profiles = profiles
            s.save()
        self._drop_pools(name)

    def _drop_pools(self, name: str) -> None:
        with self._pools_lock:
            for k in [k for k in self._pools if k[0] == name]:
                del self._pools[k]

    # -- candidate collection ------------------------------------------------------

    def _guard(self, service: str, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        """Call a provider method under its lock; a failing service is skipped."""
        try:
            with self.engine._plock(service):
                return fn(*args, **kw)
        except Exception as exc:  # noqa: BLE001 - one service failing must not sink the pick
            log.info("game source: %s call failed: %s", service, exc)
            return None

    def _streaming(self) -> list[tuple[str, Any]]:
        """Streaming providers usable right now: signed in, or public-catalogue
        services (YouTube Music works with no account). A signed-out Qobuz is
        skipped silently."""
        out = []
        for svc, prov in self.engine._ensure_providers().items():
            if svc.value == "local":
                continue
            if svc.value in PUBLIC_SERVICES or getattr(prov, "is_authenticated", False):
                out.append((svc.value, prov))
        return out

    def _library(self) -> Any | None:
        lib = self.engine._library_provider()
        return lib if lib is not None and lib.active() else None

    def _collect_one(self, sel: dict[str, Any], sources: str) -> list[Any]:
        (kind, arg), = sel.items()
        lib = self._library()
        tracks: list[Any] = []
        if kind == "genre":
            if lib is not None:
                page = self._guard("local", lib.browse_view, "songs", genre=arg, limit=GENRE_CAP,
                                   order="artist")
                tracks += (page or {}).get("songs") or []
            if sources == "all":
                for svc, prov in self._streaming():  # "<genre> music" finds the genre's songs
                    r = self._guard(svc, prov.search, f"{arg} music", kinds=("tracks",),
                                    limit=STREAM_SEARCH_LIMIT)
                    tracks += list(getattr(r, "tracks", None) or [])
        elif kind == "query":
            if lib is not None:
                r = self._guard("local", lib.search, arg, kinds=("tracks",), limit=LIB_SEARCH_LIMIT)
                tracks += list(getattr(r, "tracks", None) or [])
            if sources == "all":
                for svc, prov in self._streaming():
                    r = self._guard(svc, prov.search, arg, kinds=("tracks",), limit=STREAM_SEARCH_LIMIT)
                    tracks += list(getattr(r, "tracks", None) or [])
        else:  # playlist / album / artist: an exact service + id
            service, item_id = arg["service"], arg["id"]
            if service == "local":
                prov = lib
            elif sources == "all":
                prov = dict(self._streaming()).get(service)
            else:
                log.info("game source: sources=library skips %s selector for %s", kind, service)
                prov = None
            if prov is not None:
                fn = {"playlist": prov.get_playlist_tracks, "album": prov.get_album_tracks,
                      "artist": lambda i: prov.get_artist_top_tracks(i, limit=ARTIST_TRACKS)}[kind]
                tracks += self._guard(service, fn, item_id) or []
        return tracks

    def _collect(self, selectors: list[dict[str, Any]], sources: str) -> dict[tuple[str, str], dict[str, Any]]:
        by_song: dict[str, dict[str, Any]] = {}
        order: list[dict[str, Any]] = []
        for sel in selectors:
            for t in self._collect_one(sel, sources):
                ref = _ref(t)
                if ref is None or (sources == "library" and ref["service"] != "local"):
                    continue
                dur = ref["duration_s"]
                if ref["service"] != "local" and isinstance(dur, (int, float)) and dur \
                        and not STREAM_MIN_S <= dur <= STREAM_MAX_S:
                    continue
                # One song once, even if the Library and a service both have it
                # (the Library copy wins), so a "repeat" can't hide as a twin.
                song = f"{_norm(ref['title'])}|{_norm(ref['artist'])}" if ref["title"] else ""
                if song and song in by_song:
                    if by_song[song]["service"] != "local" and ref["service"] == "local":
                        by_song[song].update(ref)
                    continue
                if song:
                    by_song[song] = ref
                order.append(ref)
        return {(r["service"], r["id"]): r for r in order}

    def _pool(self, name: str, ttype: str, sources: str, selectors: list[dict[str, Any]]) -> TrackPool:
        key = (name, ttype, sources)
        with self._pools_lock:
            pool = self._pools.setdefault(key, TrackPool())
        fp = hashlib.sha256(json.dumps([selectors, sources], sort_keys=True).encode()).hexdigest()
        with pool.lock:
            stale = fp != pool.fingerprint or time.monotonic() - pool.built > POOL_TTL_S
            if stale:
                refs = self._collect(selectors, sources)
                if refs or fp != pool.fingerprint:
                    pool.refs = refs
                    pool.bag.sync(list(refs))
                    pool.fingerprint = fp
                # An empty refresh of an unchanged selector keeps the old pool
                # (a service blip must not silence the game's music).
                # (Retry an empty pool after 30 s rather than on every request.)
                pool.built = time.monotonic() if refs else time.monotonic() - POOL_TTL_S + 30.0
        return pool

    # -- next ---------------------------------------------------------------------

    def _resolve_type(self, profile: dict[str, Any], requested: str) -> str:
        types = profile["types"]
        if requested in types:
            return requested
        folded = {t.casefold(): t for t in types}
        if requested.casefold() in folded:
            return folded[requested.casefold()]
        if "default" in types:
            return "default"
        raise GameError(404, "unknown_type",
                        f"profile has no music type {requested!r} and no \"default\" type "
                        f"(types: {', '.join(sorted(types))})")

    def _mark_bad(self, service: str, item_id: str) -> None:
        if service != "local":
            self._bad[(service, item_id)] = time.monotonic()

    def _is_bad(self, key: tuple[str, str]) -> bool:
        at = self._bad.get(key)
        if at is None:
            return False
        if time.monotonic() - at > BAD_TTL_S:
            self._bad.pop(key, None)
            return False
        return True

    def _take(self, pool: TrackPool, count: int, consume: bool) -> list[dict[str, Any]]:
        """The next ``count`` playable candidates, skipping tracks that recently
        failed to resolve (region/age-blocked on YouTube...). Peeking skips the
        same ones, so a peek always equals the next consuming call."""
        if not consume:
            extra = sum(1 for k in pool.refs if self._is_bad(k))
            keys = [k for k in pool.bag.take(count + extra, consume=False) if not self._is_bad(k)]
            return [pool.refs[k] for k in keys[:count] if k in pool.refs]
        out: list[dict[str, Any]] = []
        for _ in range(count + len(pool.refs)):
            if len(out) >= count:
                break
            for k in pool.bag.take(1, consume=True):
                if k in pool.refs and not self._is_bad(k):
                    out.append(pool.refs[k])
        return out

    def _remember(self, track: dict[str, Any]) -> None:
        with self._meta_lock:
            key = (track["service"], track["id"])
            self._meta[key] = {k: track.get(k) for k in
                               ("service", "id", "title", "artist", "album", "duration_s", "quality")}
            self._meta.move_to_end(key)
            while len(self._meta) > META_MAX:
                self._meta.popitem(last=False)

    def _audio_url(self, service: str, item_id: str, profile: dict[str, Any]) -> str:
        f = profile["format"]
        return (f"/api/game/audio/{service}/{quote(item_id, safe='')}.wav?rate={f['rate']}"
                f"&bits={f['bits']}&channels={f['channels']}"
                f"&normalize={int(profile['normalize'])}&trim={int(profile['trim_silence'])}")

    def _publish(self, refs: list[dict[str, Any]], sources: str, profile: dict[str, Any]) -> list[dict[str, Any]]:
        """Candidate records -> API tracks (streaming ones swapped for the
        Library's copy when the engine has one, per the library-preferred setting)."""
        swap: dict[int, Any] = {}
        if sources == "all" and self.engine._library_preferred():
            todo = [i for i, r in enumerate(refs) if r["service"] != "local" and r["title"]]
            if todo:
                try:
                    found = self.engine._match_library([
                        {k: refs[i].get(k) for k in ("title", "artist", "isrc", "duration_s")}
                        for i in todo])
                except Exception as exc:  # noqa: BLE001 - matching is best effort
                    log.info("game source: library match failed: %s", exc)
                    found = [None] * len(todo)
                lib = self._library()
                for i, lib_id in zip(todo, found, strict=True):
                    if lib_id and lib is not None:
                        t = self._guard("local", lib.get_track, lib_id)
                        if t is not None:
                            swap[i] = _ref(t)
        out = []
        for i, ref in enumerate(refs):
            r = swap.get(i) or ref
            svc, tid = r["service"], r["id"]
            try:
                src = dict(self.engine._source_for(svc, tid))
            except Exception:  # noqa: BLE001
                src = {"service": svc}
            src["from_library"] = svc == "local" and ref["service"] != "local"
            track = {"service": svc, "id": tid, "title": r["title"], "artist": r["artist"],
                     "album": r["album"], "duration_s": r["duration_s"], "quality": r["quality"],
                     "source": src, "audio_url": self._audio_url(svc, tid, profile)}
            self._remember(track)
            out.append(track)
        return out

    def next_tracks(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise GameError(400, "bad_request", "body must be a JSON object")
        unknown = set(body) - {"profile", "type", "sources", "count", "peek"}
        if unknown:
            raise GameError(400, "bad_request", f"unknown key(s): {', '.join(sorted(map(str, unknown)))}")
        name = validate_name(body.get("profile"))
        requested = body.get("type")
        if not isinstance(requested, str) or not TYPE_RE.match(requested):
            raise GameError(400, "bad_request", '"type" must be a music type name such as "explore"')
        count = body.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
            raise GameError(400, "bad_request", f'"count" must be an integer 1-{MAX_COUNT}')
        peek = body.get("peek", False)
        if not isinstance(peek, bool):
            raise GameError(400, "bad_request", '"peek" must be true or false')
        profile = self.get_profile(name)
        sources = body.get("sources", profile["sources"])
        if sources not in SOURCES:
            raise GameError(400, "bad_request", '"sources" must be "library" or "all"')
        ttype = self._resolve_type(profile, requested)
        selectors = selectors_of(profile["types"][ttype])
        pool = self._pool(name, ttype, sources, selectors)
        with pool.lock:
            refs = self._take(pool, count, consume=not peek)
            size = len(pool.refs)
        if not refs:
            hint = (" (sources=library: is the Library enabled and does it hold matching music?)"
                    if sources == "library" else "")
            raise GameError(404, "no_tracks", f"no playable tracks for type {ttype!r}{hint}")
        return {"profile": name, "type": ttype, "requested_type": requested, "sources": sources,
                "peek": peek, "pool_size": size, "tracks": self._publish(refs, sources, profile)}

    # -- playing / status -------------------------------------------------------------

    def playing(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise GameError(400, "bad_request", "body must be a JSON object")
        unknown = set(body) - {"profile", "type", "service", "id"}
        if unknown:
            raise GameError(400, "bad_request", f"unknown key(s): {', '.join(sorted(map(str, unknown)))}")
        profile = validate_name(body.get("profile"))
        ttype = body.get("type")
        if not isinstance(ttype, str) or not TYPE_RE.match(ttype):
            raise GameError(400, "bad_request", '"type" must be a music type name')
        service, item_id = _check_track_ref(body.get("service"), body.get("id"))
        with self._meta_lock:
            meta = dict(self._meta.get((service, item_id)) or {})
        now = {"profile": profile, "type": ttype, "service": service, "id": item_id,
               "title": meta.get("title"), "artist": meta.get("artist"), "album": meta.get("album"),
               "duration_s": meta.get("duration_s"), "quality": meta.get("quality"),
               "started_at": time.time()}
        self._now = now
        self._history.appendleft(now)
        return {"ok": True, "now_playing": self._now_view()}

    def _now_view(self) -> dict[str, Any] | None:
        now = self._now
        if now is None:
            return None
        age = time.time() - now["started_at"]
        limit = (now.get("duration_s") or 3600) + 60  # a game that quit stops "playing"
        if age > limit:
            return None
        return {**now, "age_s": round(age, 1)}

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled(), "ffmpeg": bool(self._ffmpeg()),
                "cache": self.cache.stats(), "now_playing": self._now_view(),
                "history": [{k: h.get(k) for k in ("profile", "type", "service", "id", "title",
                                                   "artist", "started_at")}
                            for h in list(self._history)[:20]],
                "profiles": sorted(self._stored())}

    # -- audio --------------------------------------------------------------------------

    def _parse_format(self, query: Mapping[str, list[str]]) -> tuple[int, int, int, bool, bool]:
        def one(name: str) -> str | None:
            v = query.get(name)
            return v[0] if v else None

        def num(name: str, default: int, choices: tuple[int, ...]) -> int:
            raw = one(name)
            if raw is None or raw == "":
                return default
            try:
                value = int(raw)
            except ValueError:
                value = -1
            if value not in choices:
                raise GameError(400, "bad_format", f"{name} must be one of {', '.join(map(str, choices))}")
            return value

        def flag(name: str) -> bool:
            raw = one(name)
            if raw is None or raw == "":
                return False
            if raw.lower() in ("1", "true", "yes", "on"):
                return True
            if raw.lower() in ("0", "false", "no", "off"):
                return False
            raise GameError(400, "bad_format", f"{name} must be 0 or 1")

        return (num("rate", 48000, RATES), num("bits", 16, BITS), num("channels", 2, CHANNELS),
                flag("normalize"), flag("trim"))

    def audio(self, service: str, filename: str, query: Mapping[str, list[str]],
              range_header: str | None) -> GameResponse:
        if not filename.endswith(".wav"):
            raise GameError(404, "not_found", "audio is served as <id>.wav")
        service, item_id = _check_track_ref(service, filename[:-4])
        rate, bits, channels, normalize, trim = self._parse_format(query)
        wait = self.wait_s
        if query.get("wait"):
            try:
                wait = min(MAX_WAIT_S, max(0.0, float(query["wait"][0])))
            except ValueError as exc:
                raise GameError(400, "bad_format", "wait must be a number of seconds") from exc
        ffmpeg = self._ffmpeg()
        key = hashlib.sha256(
            f"{_PIPELINE}|{service}|{item_id}|{rate}|{bits}|{channels}|{int(normalize)}|{int(trim)}"
            .encode()).hexdigest()[:40]
        if ffmpeg is None and not self.cache.path_for(key).exists():
            raise GameError(503, "ffmpeg_missing", "ffmpeg is not installed on this Harmony server")

        def build(tmp: Path) -> None:
            assert ffmpeg is not None
            try:
                _svc, _id, source = self.engine._resolve_preferring_library(service, item_id)
            except KeyError as exc:
                raise GameError(404, "unknown_service", f"service {exc} is not available") from exc
            except GameError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_bad(service, item_id)  # next() stops offering it for a while
                raise GameError(502, "source_unavailable", _scrub(f"could not resolve the track: {exc}")) from exc
            transcode_to_wav(ffmpeg, ffmpeg_input_args(source.url, source.headers), tmp, rate=rate,
                             bits=bits, channels=channels, normalize=normalize, trim=trim)

        path = self.cache.get(key, build, wait)
        if path is None:
            return _json({"status": "transcoding", "retry_after_s": RETRY_AFTER_S}, 202,
                         **{"Retry-After": str(RETRY_AFTER_S)})
        size = path.stat().st_size
        headers = {"Content-Type": "audio/wav", "Accept-Ranges": "bytes", "ETag": f'"{key}"',
                   "Cache-Control": "private, max-age=3600"}
        rng = parse_range(range_header, size)
        if rng == "bad":
            return GameResponse(416, None, {**headers, "Content-Range": f"bytes */{size}",
                                            "Content-Length": "0"})
        if isinstance(rng, tuple):
            start, end = rng
            return GameResponse(206, None, {**headers, "Content-Range": f"bytes {start}-{end}/{size}",
                                            "Content-Length": str(end - start + 1)},
                                file=path, start=start, length=end - start + 1)
        return GameResponse(200, None, {**headers, "Content-Length": str(size)},
                            file=path, start=0, length=size)

    # -- dispatch ---------------------------------------------------------------------------

    def handle(self, method: str, parts: list[str], query: Mapping[str, list[str]], body: Any,
               headers: Mapping[str, str] | Any) -> GameResponse:
        """Route ``method`` + decoded path segments (``["api", "game", ...]``).
        The caller has already enforced the personal-key gate."""
        try:
            return self._route(method, parts[2:], query, body, headers)
        except GameError as exc:
            return _error(exc)
        except Exception as exc:  # noqa: BLE001 - JSON, not a dropped connection
            log.warning("game source error on %s /%s: %s", method, "/".join(parts), exc)
            return _json({"error": _scrub(str(exc)) or "internal error", "code": "internal"}, 500)

    def _route(self, method: str, p: list[str], query: Mapping[str, list[str]], body: Any,
               headers: Any) -> GameResponse:
        head = method == "HEAD"
        if method == "GET" and p == ["status"]:
            return _json(self.status())
        if p and p[0] in ("profiles", "next", "playing", "audio") and not self.enabled():
            raise GameError(403, "disabled", "the game source API is turned off "
                            "(settings: game_source_enabled)")
        if p == ["profiles"]:
            if method == "GET":
                return _json({"profiles": self.list_profiles()})
            raise GameError(405, "method_not_allowed", "use GET")
        if len(p) == 2 and p[0] == "profiles":
            name = validate_name(p[1])
            if method == "GET":
                return _json({"name": name, **self.get_profile(name)})
            if method == "PUT":
                return _json(self.put_profile(name, body))
            if method == "DELETE":
                self.delete_profile(name)
                return _json({"ok": True, "deleted": name})
            raise GameError(405, "method_not_allowed", "use GET, PUT or DELETE")
        if p == ["next"]:
            if method == "POST":
                return _json(self.next_tracks(body))
            raise GameError(405, "method_not_allowed", "use POST")
        if p == ["playing"]:
            if method == "POST":
                return _json(self.playing(body))
            raise GameError(405, "method_not_allowed", "use POST")
        if len(p) == 3 and p[0] == "audio":
            if method in ("GET", "HEAD"):
                resp = self.audio(p[1], p[2], query, headers.get("Range") if headers else None)
                if head and resp.file is not None:
                    resp.file = None
                    resp.length = 0
                return resp
            raise GameError(405, "method_not_allowed", "use GET or HEAD")
        raise GameError(404, "not_found", "unknown game API route")


def _check_track_ref(service: Any, item_id: Any) -> tuple[str, str]:
    if service not in SERVICES:
        raise GameError(400, "bad_request", f"service must be one of {', '.join(SERVICES)}")
    if isinstance(item_id, int) and not isinstance(item_id, bool):
        item_id = str(item_id)
    if not isinstance(item_id, str) or not ID_RE.match(item_id) or ".." in item_id:
        raise GameError(400, "bad_request", "invalid track id")
    return service, item_id


def _load_settings() -> Any:
    from harmony.config import Settings

    return Settings.load()


def game_source_for(engine: Any) -> GameSource:
    """The engine's (lazily created, process-wide) :class:`GameSource`."""
    with _INIT_LOCK:
        gs = engine.__dict__.get("_game_source")
        if gs is None:
            gs = engine.__dict__["_game_source"] = GameSource(engine)
        return gs


_INIT_LOCK = threading.Lock()


# -- HTTP glue ------------------------------------------------------------------------------


def send_response(handler: Any, resp: GameResponse, *, head: bool = False) -> None:
    """Write ``resp`` to a ``BaseHTTPRequestHandler``. JSON bodies carry an exact
    Content-Length; audio streams the file range from disk."""
    try:
        if resp.file is None:
            raw = b"" if resp.body is None else json.dumps(resp.body).encode("utf-8")
            handler.send_response(resp.status)
            headers = dict(resp.headers)
            if resp.body is not None:
                headers.setdefault("Content-Type", "application/json; charset=utf-8")
                headers["Content-Length"] = str(len(raw))
            else:
                headers.setdefault("Content-Length", "0")
            for k, v in headers.items():
                handler.send_header(k, v)
            handler.end_headers()
            if not head:
                handler.wfile.write(raw)
            return
        handler.send_response(resp.status)
        for k, v in resp.headers.items():
            handler.send_header(k, v)
        handler.end_headers()
        if head:
            return
        with open(resp.file, "rb") as fh:
            fh.seek(resp.start)
            left = resp.length
            while left > 0:
                chunk = fh.read(min(256 * 1024, left))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                left -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True  # the game dropped the download; nothing to do
