"""Game source: Harmony as a proxy music source for a game engine.

A game (e.g. a Skyrim SKSE plugin) asks Harmony for "a track for the *combat*
music type", downloads it as plain PCM WAV and plays it with its own audio
engine. Harmony does the picking (profiles + shuffled "bags"), the sourcing
(Library, Qobuz, YouTube Music through the engine's providers) and the
transcoding (ffmpeg -> exact-length RIFF/WAVE or lossless FLAC, cached). The HTTP surface is
documented in ``docs/api/game-source.md``; this module is the whole
implementation, kept gi-free and independent of the HTTP framework:

* :func:`validate_profile` / :func:`validate_name` -- strict profile schema.
* :class:`TrackPool` -- candidate tracks for a (profile, type) + the bag.
* :class:`AudioCache` -- bounded, single-flight ffmpeg transcode + LRU WAV/FLAC cache.
* :class:`GameSource` -- the request router the web server delegates to
  (:meth:`GameSource.handle`) returning a :class:`GameResponse`;
  :func:`send_response` writes one to a ``BaseHTTPRequestHandler``.

The engine is used through a small duck-typed surface (all on
``harmony.web.api.Engine``): ``_library_provider()``, ``_ensure_providers()``,
``_provider(service)``, ``_plock(service)``, ``_library_preferred()``,
``_match_library(items)``, ``_resolve_preferring_library(service, id)``,
``_source_for(service, id)`` and (optional) ``_prefer_library(service, id)``, a
network-free lookup of the Library copy that will be transcoded instead of a
streaming id, used to pick the audio-cache slot. Tests substitute a fake with
those methods.
"""

from __future__ import annotations

import array
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
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from harmony.errors import AuthError, ProviderError, RateLimitedError
from harmony.streamio import file_url_path, is_file_url

log = logging.getLogger(__name__)

# -- limits / constants -----------------------------------------------------------

SOURCES = ("library", "all")
#: ``sources`` may also be a comma-separated subset of these (canonical order), e.g. "library,ytmusic".
SOURCE_NAMES = ("library", "ytmusic", "qobuz")
_SOURCE_SERVICE = {"library": "local", "ytmusic": "ytmusic", "qobuz": "qobuz"}
#: What a request/profile that sets no ``sources`` means: every ACTIVE source (the
#: Library when there is one, YouTube Music always, Qobuz only when signed in; an
#: unusable one is skipped silently). ``"library"`` must be asked for.
DEFAULT_SOURCES = "all"
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
#: Audio file formats: ``wav`` (canonical PCM, byte offsets map to sample frames) and
#: ``flac`` (the same PCM, losslessly compressed; the sources are lossy already, so
#: there is deliberately no lossy container).
CONTAINERS = ("wav", "flac")
DEFAULT_FORMAT = {"rate": 48000, "bits": 16, "channels": 2, "container": "wav"}
FLAC_LEVEL = 1                # ffmpeg -compression_level: ~5% bigger than 5, same encode speed

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
MAX_PROFILES = 32
MAX_PROFILE_BYTES = 32 * 1024         # one serialised profile
MAX_PROFILES_BYTES = 256 * 1024       # all stored profiles together

POOL_TTL_S = 600.0            # candidate list refresh interval (library-only pools; also the memo lifetime)
DEFAULT_STREAM_POOL_TTL_S = 3600.0   # ... for pools with streaming selectors (setting game_stream_pool_ttl_s)
MIN_STREAM_POOL_TTL_S = 600.0
MAX_STREAM_POOL_TTL_S = 86400.0
EMPTY_POOL_RETRY_S = 3.0      # an empty candidate list (Library mid-scan...) is retried this soon
STREAM_EMPTY_RETRY_S = 30.0   # ... an empty STREAMING list this soon (every try is a web request)
MAX_PARTIAL_ATTEMPTS = 5      # consecutive partial builds before the fetched-results memo is dropped
BACKOFF_MIN_S = 30.0          # per-service back-off after a rate limit / BACKOFF_FAILS failures in a row
BACKOFF_MAX_S = 600.0
BACKOFF_FAILS = 3
HISTORY_WARN_S = 600.0        # a history write failure is logged at most this often
GENRE_CAP = 1000              # library songs pulled for a genre selector
LIB_SEARCH_LIMIT = 50
ARTIST_TRACKS = 100
DEFAULT_STREAM_DEPTH = 100    # tracks per streaming query for genre/query selectors (setting)
MIN_STREAM_DEPTH = 10
MAX_STREAM_DEPTH = 300
STREAM_PAGE = {"qobuz": 50}   # services paged with limit+offset; others get one call (ytmusicapi pages itself)
STREAM_BUDGET_S = 8.0         # time a pool build may spend on streaming requests (beyond: partial pool)
PARTIAL_POOL_RETRY_S = 60.0   # a budget-truncated / partly failed list is rebuilt this soon
MOOD_PLAYLISTS = 3            # YouTube Music mood/genre playlists added for a matching genre name
MOOD_TRACKS = 50              # ... and the tracks taken from each
HISTORY_FILE = "game-history.json"
HISTORY_VERSION = 1
HISTORY_POOL_MAX = 2000       # served keys remembered per (profile, type, sources)
HISTORY_TOTAL_MAX = 20000     # ... and over all pools (oldest pools dropped first)
RECENT_MAX = 50               # a new cycle never starts with one of this many latest served tracks

DEFAULT_CACHE_MB = 2048
MIN_CACHE_MB = 64
DEFAULT_WAIT_S = 30.0         # how long an audio request blocks while transcoding
MAX_WAIT_S = 120.0
RETRY_AFTER_S = 2
TRANSCODE_TIMEOUT_S = 900
MAX_AUDIO_S = 2 * 60 * 60     # longer sources are cut (keeps RIFF sizes < 4 GiB)
FAIL_TTL_S = 20.0             # a failed transcode is not retried for this long
MAX_FAILED = 256              # remembered failures (oldest dropped)
MAX_BAD = 5000                # remembered unresolvable tracks (oldest dropped)
MAX_BUILDS = 3                # concurrent transcodes (ffmpeg + yt-dlp are heavy)
MAX_QUEUED_BUILDS = 32        # builds waiting for a slot; beyond this -> 503 busy
MIN_EVICT_AGE_S = 120.0       # a cache entry used/made this recently is never evicted
PIN_MAX_S = 3600.0            # a serve pin older than this is treated as leaked
SILENCE_DB = -50
HISTORY_MAX = 50
META_MAX = 5000

#: Loudness normalisation target (EBU R128, single pass ``loudnorm``).
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
_SILENCE = f"silenceremove=start_periods=1:start_threshold={SILENCE_DB}dB:start_duration=0.02"
_PIPELINE = "v2"              # bump to invalidate the WAV cache when the pipeline changes


class GameError(Exception):
    """A request-level error rendered as ``{"error", "code"}`` JSON."""

    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


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


def normalize_sources(value: Any, path: str) -> str:
    """``"all"``, ``"library"`` or a comma-separated subset of library/ytmusic/qobuz,
    returned in canonical order without duplicates (``"qobuz, library,library"`` ->
    ``"library,qobuz"``)."""
    if value in SOURCES:
        return value
    if not isinstance(value, str):
        raise _bad(path, 'must be "all", "library" or a comma-separated list of '
                   f"{', '.join(SOURCE_NAMES)}")
    names = [n.strip().lower() for n in value.split(",")]
    bad = [n for n in names if n not in SOURCE_NAMES]
    if bad:
        raise _bad(path, f"unknown source {', '.join(repr(n) for n in bad)}; use \"all\", \"library\" or "
                   f"a comma-separated list of {', '.join(SOURCE_NAMES)}")
    return ",".join(n for n in SOURCE_NAMES if n in names)


def _int_choice(value: Any, choices: tuple[int, ...], path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in choices:
        raise _bad(path, f"must be one of {', '.join(map(str, choices))}")
    return value


def _container(value: Any, path: str) -> str:
    if not isinstance(value, str) or value not in CONTAINERS:
        raise _bad(path, 'must be "wav" or "flac"')
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
    sources = normalize_sources(data.get("sources", DEFAULT_SOURCES), "sources")
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
        "container": _container(fmt_in.get("container", "wav"), "format.container"),
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


def flac_valid(path: Path) -> bool:
    """Whether ``path`` is a complete FLAC written by us: the ``fLaC`` marker, a
    STREAMINFO first block, a clean walk over the metadata blocks to the one flagged
    last, then a frame sync, and at least as many bytes as STREAMINFO's sample
    count needs (<= 65535 samples and >= 8 bytes per frame). Files appear only by
    rename after ffmpeg exited 0, so this catches foreign or truncated files."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(42)
            if size <= 42 or head[:4] != b"fLaC" or head[4] & 0x7F != 0 or head[5:8] != b"\x00\x00\x22":
                return False
            total = int.from_bytes(head[21:26], "big") & 0xFFFFFFFFF
            pos, last = 4, head[4] & 0x80
            while True:
                fh.seek(pos)
                block = fh.read(4)
                if len(block) < 4:
                    return False
                last = block[0] & 0x80
                pos += 4 + int.from_bytes(block[1:4], "big")
                if last:
                    break
            fh.seek(pos)
            sync = fh.read(2)
    except OSError:
        return False
    return (len(sync) == 2 and sync[0] == 0xFF and sync[1] & 0xFE == 0xF8
            and size - pos >= (-(-total // 65535) * 8 if total else 8))


# -- ffmpeg -----------------------------------------------------------------------


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def ffmpeg_input_args(url: str, headers: Mapping[str, str] | None = None) -> list[str]:
    """``-i`` arguments for an engine-resolved source: a library ``file://`` or
    an http(s) stream with its request headers. Nothing else is accepted, and
    ffmpeg's protocol whitelist stops a playlist/manifest pulling in other
    schemes."""
    # ``-t`` before ``-i`` is an INPUT limit: ffmpeg stops reading (and downloading) the
    # source after MAX_AUDIO_S; the output-side ``-t`` alone would not bound the input.
    cut = ["-t", str(MAX_AUDIO_S)]
    if is_file_url(url):
        return ["-protocol_whitelist", "file", *cut, "-i", str(file_url_path(url))]
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
    return args + [*cut, "-i", url]


def build_filter(rate: int, bits: int, channels: int, normalize: bool, trim: bool) -> str:
    """The ffmpeg ``-af`` chain. ``trim`` only strips the LEADING silence here (a
    streaming filter); the tail is trimmed on the finished PCM by
    :func:`trim_tail_silence`. (``areverse`` would hold the whole decoded source
    in memory -- tens of MB per minute -- to do the same.)"""
    chain: list[str] = []
    if trim:
        chain.append(_SILENCE)
    if normalize:
        chain.append(LOUDNORM)
    if channels == 1:
        chain.append("aformat=channel_layouts=mono")
    else:
        chain.append("aformat=channel_layouts=stereo")
    # The final stage owns the rate/sample-format conversion so 16-bit gets dither.
    chain.append(f"aresample=osr={rate}:osf={'s16' if bits == 16 else 's32'}:dither_method=triangular_hp")
    return ",".join(chain)


def trim_tail_silence(f: Any, data_start: int, data_len: int, bits: int, channels: int) -> int:
    """Length in bytes of the PCM at ``data_start`` once trailing near-silence
    (below ``SILENCE_DB`` on every channel) is dropped; the file is only read, in
    chunks from the end, so memory stays flat however long the track is. Returns
    ``0`` when the whole thing is silent. 24-bit is compared on its top 16 bits."""
    block = channels * bits // 8
    thr = int(32768 * 10 ** (SILENCE_DB / 20))
    chunk_frames = 1 << 16
    end = data_len // block  # frames not yet ruled out
    while end > 0:
        n = min(chunk_frames, end)
        begin = end - n
        f.seek(data_start + begin * block)
        raw = f.read(n * block)
        if bits == 16:
            samples = array.array("h")
            samples.frombytes(raw[: n * block])
        else:  # keep the high 16 bits of each little-endian 24-bit sample
            hi = bytearray(2 * n * channels)
            hi[0::2] = raw[1::3]
            hi[1::2] = raw[2::3]
            samples = array.array("h")
            samples.frombytes(bytes(hi))
        if sys.byteorder == "big":
            samples.byteswap()
        if max(samples) > thr or min(samples) < -thr:
            for i in range(len(samples) - 1, -1, -1):
                if abs(samples[i]) > thr:
                    return (begin + i // channels + 1) * block
        end = begin
    return 0


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
        if trim and data_len > 0:
            with open(dest, "rb") as rd:
                data_len = trim_tail_silence(rd, 44, data_len, bits, channels)
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


def transcode_to_flac(ffmpeg: str, in_args: list[str], dest: Path, *, rate: int, bits: int,
                      channels: int, normalize: bool, trim: bool) -> int:
    """Like :func:`transcode_to_wav` but ``dest`` ends up a lossless FLAC (returns the
    PCM byte count). The canonical WAV is produced first -- one pipeline, so the
    normalisation, trim, dither and depth rules cannot drift between containers --
    and then only re-wrapped losslessly; the scratch WAV is removed again."""
    scratch = dest.with_name(dest.name + ".wav.part")
    try:
        data_len = transcode_to_wav(ffmpeg, in_args, scratch, rate=rate, bits=bits,
                                    channels=channels, normalize=normalize, trim=trim)
        # ffmpeg decodes 24-bit PCM as s32; bits_per_raw_sample keeps the FLAC 24-bit.
        depth = ["-sample_fmt", "s16"] if bits == 16 else ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
        cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "wav",
               "-i", str(scratch), "-vn", "-map_metadata", "-1", "-c:a", "flac",
               "-compression_level", str(FLAC_LEVEL), "-fflags", "+bitexact", *depth, "-f", "flac", str(dest)]
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, our own scratch file
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=TRANSCODE_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired as exc:
            raise GameError(504, "transcode_timeout", "ffmpeg took too long") from exc
        if proc.returncode != 0:
            tail = _scrub(proc.stderr.decode("utf-8", "replace").strip()[-300:])
            raise GameError(502, "transcode_failed", f"ffmpeg failed (rc={proc.returncode}): {tail}")
        with open(dest, "rb") as fh:
            os.fsync(fh.fileno())
        return data_len
    finally:
        with contextlib.suppress(OSError):
            scratch.unlink()


# -- audio cache --------------------------------------------------------------------


@dataclass
class _Job:
    done: threading.Event = field(default_factory=threading.Event)
    error: GameError | None = None
    key: str = ""             # the key the finished file was stored under (see ``build``)


class AudioCache:
    """Finished WAV/FLAC files on disk (LRU by mtime, size-capped) + single-flight builds.

    ``get(key, build, wait_s)``: a hit returns the path at once; a miss starts
    ``build(tmp_path)`` on a worker thread (so a client giving up never kills a
    transcode) shared by every concurrent caller of the same key, and blocks up
    to ``wait_s`` for it. ``None`` means "still working" (the HTTP layer answers
    202 + Retry-After). Files appear atomically (temp + rename).

    ``build`` may return a different key than it was started for (when the
    source that actually got used differs from the one predicted, e.g. a
    Library copy that turned out unavailable); the file is then stored under
    that key so a cached entry always matches the audio it holds.

    Resource limits: at most ``MAX_BUILDS`` transcodes run at once (the rest
    wait, their callers get 202) and at most ``MAX_QUEUED_BUILDS`` wait (more ->
    503 ``busy``). Half-written ``.part`` files count toward the size cap, and a
    new build is refused (503 ``cache_full``) while the cache is over the cap and
    nothing can be evicted. An entry that was made/used in the last
    ``min_evict_age_s`` or is being served (pinned) is never evicted; the
    effective cap is never below twice the largest cached file, so two jobs
    cannot keep evicting each other under a tiny cap.
    """

    def __init__(self, directory: Path, cap_bytes: Callable[[], int], *,
                 max_builds: int = MAX_BUILDS, max_queued: int = MAX_QUEUED_BUILDS,
                 min_evict_age_s: float = MIN_EVICT_AGE_S) -> None:
        self.dir = Path(directory)
        self._cap = cap_bytes
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._failed: collections.OrderedDict[str, tuple[float, GameError]] = collections.OrderedDict()
        self._slots = threading.BoundedSemaphore(max_builds)
        self._max_builds = max_builds
        self._max_queued = max_queued
        self.min_evict_age_s = min_evict_age_s
        self._pins: dict[str, list[float]] = {}
        self.dir.mkdir(parents=True, exist_ok=True)
        self._sweep_parts()

    def path_for(self, key: str) -> Path:
        """A key is a hash (a WAV, ``<key>.wav``) or ``<hash>.flac`` (a FLAC, stored as is)."""
        return self.dir / (key if key.endswith(".flac") else f"{key}.wav")

    def _sweep_parts(self) -> None:
        """Remove half-written temp files left by a crashed run."""
        cutoff = time.time() - 3600
        for p in self.dir.glob("*.part"):
            with contextlib.suppress(OSError):
                if p.stat().st_mtime < cutoff:
                    p.unlink()

    def _sizes(self, pattern: str) -> list[tuple[Path, float, int]]:
        out = []
        for p in self.dir.glob(pattern):
            with contextlib.suppress(OSError):
                st = p.stat()
                out.append((p, st.st_mtime, st.st_size))
        return out

    def _finished(self) -> list[tuple[Path, float, int]]:
        return self._sizes("*.wav") + self._sizes("*.flac")

    def stats(self) -> dict[str, int]:
        wavs, parts = self._finished(), self._sizes("*.part")
        return {"bytes": sum(s for _p, _m, s in wavs), "files": len(wavs),
                "in_progress_bytes": sum(s for _p, _m, s in parts), "cap": int(self._cap())}

    # -- pinning (a file being served must not be evicted) ---------------------

    def pin(self, path: Path) -> None:
        with self._lock:
            self._pins.setdefault(path.name, []).append(time.monotonic())

    def unpin(self, path: Path) -> None:
        with self._lock:
            held = self._pins.get(path.name)
            if held:
                held.pop(0)
                if not held:
                    del self._pins[path.name]

    def _pinned(self, name: str) -> bool:
        cutoff = time.monotonic() - PIN_MAX_S  # a pin whose release never came expires
        return any(t > cutoff for t in self._pins.get(name, ()))

    # -- lookup / build ---------------------------------------------------------

    def _hit(self, key: str) -> Path | None:
        p = self.path_for(key)
        if not p.exists():
            return None
        if not (flac_valid(p) if p.suffix == ".flac" else wav_valid(p)):
            log.warning("game-audio: discarding invalid cache file %s", p.name)
            with contextlib.suppress(OSError):
                p.unlink()
            return None
        with contextlib.suppress(OSError):
            os.utime(p)  # LRU touch
        return p

    def get(self, key: str, build: Callable[[Path], str | None], wait_s: float) -> Path | None:
        """The cached file for ``key`` (pinned: the caller must :meth:`unpin` it
        once it has finished serving), or ``None`` while it is still being built."""
        with self._lock:
            hit = self._hit(key)
            if hit is not None:
                self._pins.setdefault(hit.name, []).append(time.monotonic())
                return hit
            failed = self._failed.get(key)
            if failed and time.monotonic() - failed[0] < FAIL_TTL_S:
                raise failed[1]
            job = self._jobs.get(key)
            if job is None:
                if len(self._jobs) >= self._max_queued + self._max_builds:
                    raise GameError(503, "busy", "too many audio conversions are queued; retry shortly",
                                    retry_after=RETRY_AFTER_S)
                self._check_room()
                job = self._jobs[key] = _Job(key=key)
                threading.Thread(target=self._run, args=(key, job, build), daemon=True,
                                 name=f"game-audio-{key[:8]}").start()
        job.done.wait(max(0.0, wait_s))
        if not job.done.is_set():
            return None
        if job.error is not None:
            raise job.error
        with self._lock:
            hit = self._hit(job.key)
            if hit is None:  # evicted between finishing and now: report as still working
                return None
            self._pins.setdefault(hit.name, []).append(time.monotonic())
            return hit

    def _effective_cap(self, wavs: list[tuple[Path, float, int]]) -> int:
        largest = max((s for _p, _m, s in wavs), default=0)
        return max(int(self._cap()), 2 * largest)

    def _check_room(self) -> None:
        """Refuse a new build while the cache (finished files + in-progress
        ``.part`` files) is over its cap and nothing more can be evicted."""
        wavs, parts = self._finished(), self._sizes("*.part")
        if sum(s for _p, _m, s in wavs) + sum(s for _p, _m, s in parts) <= self._effective_cap(wavs):
            return
        self._evict(extra=sum(s for _p, _m, s in parts))
        wavs, parts = self._finished(), self._sizes("*.part")
        if sum(s for _p, _m, s in wavs) + sum(s for _p, _m, s in parts) > self._effective_cap(wavs):
            raise GameError(503, "cache_full",
                            "the game audio cache is full of files in use or made moments ago and cannot "
                            "take another conversion yet; retry shortly or raise game_cache_max_mb",
                            retry_after=RETRY_AFTER_S * 5)

    def _run(self, key: str, job: _Job, build: Callable[[Path], str | None]) -> None:
        tmp = self.dir / f"{key}.{os.getpid()}.{threading.get_ident()}.part"
        try:
            with self._slots:
                actual = build(tmp) or key
                final = self.path_for(actual)
                os.replace(tmp, final)
                job.key = actual
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
                    self._failed.move_to_end(key)
                    self._trim_failed()
                else:
                    self._failed.pop(key, None)
            job.done.set()

    def _trim_failed(self) -> None:
        cutoff = time.monotonic() - FAIL_TTL_S
        for k in [k for k, (at, _e) in self._failed.items() if at < cutoff]:
            del self._failed[k]
        while len(self._failed) > MAX_FAILED:
            self._failed.popitem(last=False)

    def _evict(self, keep: Path | None = None, extra: int = 0) -> None:
        """Delete least-recently-used finished files until the cache (plus
        ``extra`` bytes of in-progress work) is under the cap. Pinned, just-used
        and ``keep`` entries are spared."""
        with self._lock:
            wavs = self._finished()
            cap = self._effective_cap(wavs)
            total = sum(s for _p, _m, s in wavs) + extra
            now = time.time()
            for p, mtime, size in sorted(wavs, key=lambda w: w[1]):
                if total <= cap:
                    break
                if p == keep or self._pinned(p.name) or now - mtime < self.min_evict_age_s:
                    continue
                with contextlib.suppress(OSError):
                    p.unlink()
                    total -= size


# -- candidate pools + bag ------------------------------------------------------------


# Same-song detection across services and query variants. Deliberately conservative: only
# bracketed/dash-suffix tags made purely of "noise" words are dropped, so "(Live)", "(Remix)",
# "(Acoustic)", "(Instrumental)", "(Demo)", "(Radio Edit)" keep their song apart from the original.
_NOISE_WORDS = frozenset({"official", "music", "video", "audio", "lyric", "lyrics", "visualizer",
                          "visualiser", "hd", "hq", "4k", "remaster", "remastered", "version", "explicit",
                          "clean", "mv", "m/v", "with", "full", "song"})
_NOISE_STRONG = _NOISE_WORDS - {"music", "version", "with", "full", "song"}  # one of these must be present
_TAG_BRACKET = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]")
_TAG_DASH = re.compile(r"\s+[-\u2013\u2014]\s+([^-\u2013\u2014]*)$")
_FEAT_BRACKET = re.compile(r"^(?:feat\.?|ft\.?|featuring)\s")
_FEAT_TAIL = re.compile(r"\s+(?:feat\.|ft\.|featuring)\s.*$")


def _is_noise_tag(text: str) -> bool:
    words = [w for w in re.split(r"[^\w/]+", text) if w]
    if not words or not all(w in _NOISE_WORDS or re.fullmatch(r"\d{4}", w) for w in words):
        return False
    return any(w in _NOISE_STRONG for w in words)


def _fold(text: Any) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def _song_key(title: Any, artist: Any) -> str:
    """``"<title>|<artist>"`` normalised for same-song comparison, or "" when either side
    is empty (then the track is never merged with anything)."""
    t = _TAG_BRACKET.sub(
        lambda m: "" if _FEAT_BRACKET.match(m.group(1).strip()) or _is_noise_tag(m.group(1)) else m.group(0),
        _fold(title))
    dash = _TAG_DASH.search(t)
    if dash and _is_noise_tag(dash.group(1)):
        t = t[:dash.start()]
    t = _FEAT_TAIL.sub("", t)
    a = _FEAT_TAIL.sub("", re.sub(r"\s+-\s+topic$", "", _fold(artist)))
    t, a = re.sub(r"[\W_]+", " ", t).strip(), re.sub(r"[\W_]+", " ", a).strip()
    return f"{t}|{a}" if t and a else ""


_SERVICE_RANK = {"local": 0}  # which copy of one song wins: the Library's; between streaming services the first seen


def _transient(exc: BaseException, exact: bool = False) -> bool:
    """Whether a failed streaming call is the service's / network's fault (and may well pass):
    rate limits, network, timeouts, auth, HTTP 5xx/401/408/429 (and 403 on a search). Anything
    else is a permanent failure of that call: a deleted playlist, a changed page layout, HTTP 404,
    a ProviderError without an HTTP status (a payload of the wrong shape), and 403 on an exact-id
    selector (``exact``: a private or region-locked playlist)."""
    if isinstance(exc, (RateLimitedError, AuthError, OSError, TimeoutError)) or isinstance(exc.__cause__, OSError):
        return True
    if isinstance(exc, ProviderError):
        m = re.search(r"\b([45]\d\d)\b", str(exc))
        code = int(m.group(1)) if m else 0
        return code >= 500 or code in (401, 408, 429) or (code == 403 and not exact)
    return False


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

    The upcoming order is materialised ahead of time, so a peek returns exactly
    what a later consuming call will return (see :meth:`pick`)."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()  # noqa: S311 - shuffling music, not crypto
        self.members: list[tuple[str, str]] = []
        self.queue: collections.deque[tuple[str, str]] = collections.deque()
        self.last: tuple[str, str] | None = None      # last key taken off the queue
        self.last_out: tuple[str, str] | None = None  # last key actually handed out
        self.recent: collections.deque[tuple[str, str]] = collections.deque(maxlen=RECENT_MAX)

    def _avoid_first(self, cycle: list[tuple[str, str]], prior: list[tuple[str, str]]) -> None:
        """Swap ``cycle``'s head so it is not one of the last ``min(50, members // 2)``
        keys of ``prior`` (what is served before this cycle starts)."""
        n = min(RECENT_MAX, len(self.members) // 2)
        if n < 1 or len(cycle) < 2:
            return
        avoid = set(prior[-n:])
        if cycle[0] in avoid:
            ok = [i for i, k in enumerate(cycle) if k not in avoid]
            if ok:
                j = self.rng.choice(ok)
                cycle[0], cycle[j] = cycle[j], cycle[0]

    def sync(self, keys: list[tuple[str, str]], served: list[tuple[str, str]] | None = None) -> None:
        """Adopt a (possibly changed) candidate set without restarting the cycle.

        ``served`` is the persisted memory, oldest first. A fresh bag starts with the
        never-served candidates shuffled, followed by every served one, least recently
        served first (one full least-recently-served cycle), and never opens with one
        of the last few served. Later, a newcomer that was served before (a pool that
        completes after a partial build) joins after the current cycle in the same
        least-recently-served order; only a truly unseen one is spliced in at random."""
        old = set(self.members)
        new = set(keys)
        self.members = list(dict.fromkeys(keys))
        history = list(dict.fromkeys([*(served or ()), *self.recent]))   # oldest first
        rank = {k: i for i, k in enumerate(history)}
        if not old:
            seen = [k for k in history if k in new]
            self.recent.extend(seen[-RECENT_MAX:])
            fresh = [k for k in self.members if k not in rank]
            cycle = self.rng.sample(fresh, len(fresh)) + seen
            self._avoid_first(cycle, seen)
            self.queue = collections.deque(cycle)
            return
        kept = [k for k in self.queue if k in new]
        later = sorted((k for k in self.members if k not in old and k in rank), key=rank.__getitem__)
        for k in self.members:
            if k not in old and k not in rank:  # unseen newcomers go to a random spot in what's left
                kept.insert(self.rng.randint(0, len(kept)), k)
        self.queue = collections.deque(kept + later)

    def _fill(self, n: int) -> None:
        while len(self.queue) < n and self.members:
            cycle = self.rng.sample(self.members, len(self.members))
            tail = [self.last] if self.last and (not self.recent or self.recent[-1] != self.last) else []
            prior = [*self.recent, *tail, *self.queue]
            self._avoid_first(cycle, prior)
            self.queue.extend(cycle)

    def take(self, n: int, *, consume: bool) -> list[tuple[str, str]]:
        self._fill(n)
        items = list(self.queue)[:n]
        if consume:
            for _ in items:
                self.queue.popleft()
            if items:
                self.last = self.last_out = items[-1]
                self.recent.extend(items)
        return items

    def pick(self, count: int, playable: Callable[[tuple[str, str]], bool], *, repeat_ok: bool,
             consume: bool) -> list[tuple[str, str]]:
        """The next ``count`` keys for which ``playable`` holds, walking the
        queue in order. A key equal to the one handed out just before it is
        skipped unless ``repeat_ok`` (the pool has a single playable track), so
        a small pool still never repeats a track back to back. Stops after
        ``(count + 1) * (members + 1)`` queue entries, which always suffices when
        something is playable (a partial cycle plus ``count`` full ones).

        ONE algorithm for peek and consume: it walks a cursor over the (append
        only) materialised queue and keeps its own ``prev``; with ``consume=False``
        nothing is popped and ``last``/``last_out`` stay put, so the same call with
        ``consume=True`` returns exactly the same keys."""
        out: list[tuple[str, str]] = []
        prev = self.last_out
        limit = (count + 1) * (len(self.members) + 1)
        i = 0
        while len(out) < count and i < limit:
            self._fill(i + 1)
            if i >= len(self.queue):  # no members at all
                break
            key = self.queue[i]
            i += 1
            if not playable(key) or (key == prev and not repeat_ok):
                continue
            out.append(key)
            prev = key
        if consume and i:
            taken = [self.queue.popleft() for _ in range(i)]
            self.last = taken[-1]
            if out:
                self.last_out = out[-1]
                self.recent.extend(out)
        return out


class TrackPool:
    """Candidate tracks for one (profile, type, sources) + its bag."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.lock = threading.Lock()
        self.fingerprint = ""
        self.refs: dict[tuple[str, str], dict[str, Any]] = {}
        self.bag = Bag(rng)
        # Streaming results of a budget-truncated build, keyed by request, so the next
        # refresh spends its budget on what is still missing (see GameSource._collect).
        self.memo: dict[tuple, list[Any]] = {}
        self.memo_fp = ""
        self.memo_at = 0.0
        self.attempts = 0                 # consecutive partial builds
        self.expires = 0.0                # monotonic time the candidate list is next refreshed
        self.refreshing = False           # a background refresh is running (single flight)
        self.worker: threading.Thread | None = None


class History:
    """Recently served track keys per ``"<profile>/<type>/<sources>"``, persisted to one
    small JSON file next to the settings so a restart or a rebuilt pool does not start a
    fresh shuffle. Bounded (:data:`HISTORY_POOL_MAX` per pool, :data:`HISTORY_TOTAL_MAX`
    overall, oldest dropped); an entry carries the pool's selector fingerprint and is
    discarded when that changes. Every failure (corrupt file, unwritable directory) is
    swallowed: the memory then lives in RAM only. ``peek`` never reaches it."""

    def __init__(self, path: Callable[[], Path], enabled: Callable[[], bool]) -> None:
        self._path = path
        self._enabled = enabled
        self._lock = threading.Lock()
        self._pools: dict[str, dict[str, Any]] | None = None   # loaded lazily
        self._dirty = False
        self._warned = -HISTORY_WARN_S

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._pools is not None:
            return self._pools
        pools: dict[str, dict[str, Any]] = {}
        try:
            data = json.loads(self._path().read_text("utf-8"))
            if isinstance(data, dict) and data.get("version") == HISTORY_VERSION \
                    and isinstance(data.get("pools"), dict):
                for pkey, ent in data["pools"].items():
                    if not (isinstance(pkey, str) and isinstance(ent, dict) and isinstance(ent.get("fp"), str)
                            and isinstance(ent.get("served"), list)):
                        continue
                    served: collections.OrderedDict[tuple[str, str], None] = collections.OrderedDict()
                    for raw in ent["served"][-HISTORY_POOL_MAX:]:
                        svc, _, item = raw.partition(":") if isinstance(raw, str) else ("", "", "")
                        if svc and item:
                            served[(svc, item)] = None
                    t = ent.get("t")
                    pools[pkey] = {"fp": ent["fp"], "served": served,
                                   "t": t if isinstance(t, (int, float)) else 0.0}
        except Exception:  # noqa: BLE001 - missing / corrupt / absurdly nested (RecursionError): start fresh
            pools = {}
        self._pools = pools
        return pools

    def served(self, pkey: str, fp: str) -> list[tuple[str, str]]:
        """Served keys, oldest first. An entry of a different fingerprint is reset."""
        if not self._enabled():
            return []
        with self._lock:
            pools = self._load()
            ent = pools.get(pkey)
            if ent is not None and ent["fp"] != fp:
                del pools[pkey]
                self._dirty = True
                ent = None
            return list(ent["served"]) if ent else []

    def record(self, pkey: str, fp: str, keys: list[tuple[str, str]]) -> None:
        if not keys or not self._enabled():
            return
        with self._lock:
            pools = self._load()
            ent = pools.setdefault(pkey, {"fp": fp, "served": collections.OrderedDict(), "t": 0.0})
            if ent["fp"] != fp:
                ent.update(fp=fp, served=collections.OrderedDict())
            for k in keys:
                ent["served"][k] = None
                ent["served"].move_to_end(k)
            while len(ent["served"]) > HISTORY_POOL_MAX:
                ent["served"].popitem(last=False)
            ent["t"] = time.time()
            total = sum(len(e["served"]) for e in pools.values())
            for old in sorted(pools, key=lambda p: pools[p]["t"]):   # oldest pools go first
                if total <= HISTORY_TOTAL_MAX or old == pkey:
                    break
                total -= len(pools.pop(old)["served"])
            self._dirty = True

    def forget(self, profile: str, ttype: str | None = None) -> int:
        """Drop the memory of a profile (or one of its types); the number of pools cleared."""
        with self._lock:
            pools = self._load()
            gone = [p for p in pools if p.split("/")[0] == profile
                    and (ttype is None or p.split("/")[1].casefold() == ttype.casefold())]
            for p in gone:
                del pools[p]
            if gone:
                self._dirty = True
        self.flush()
        return len(gone)

    def flush(self) -> None:
        with self._lock:
            if not self._dirty or self._pools is None or not self._enabled():
                return
            payload = {"version": HISTORY_VERSION, "pools": {
                p: {"fp": e["fp"], "t": e["t"], "served": [f"{s}:{i}" for s, i in e["served"]]}
                for p, e in self._pools.items()}}
            try:
                path = self._path()
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                try:
                    tmp.write_text(json.dumps(payload, separators=(",", ":")), "utf-8")
                    os.replace(tmp, path)
                except BaseException:
                    with contextlib.suppress(OSError):
                        tmp.unlink()
                    raise
                self._dirty = False
            except OSError as exc:
                if time.monotonic() - self._warned >= HISTORY_WARN_S:
                    self._warned = time.monotonic()
                    log.warning("game source: cannot write %s: %s", HISTORY_FILE, exc)


# -- the router ---------------------------------------------------------------------


@dataclass
class GameResponse:
    status: int = 200
    body: Any = None                      # JSON-serialisable
    headers: dict[str, str] = field(default_factory=dict)
    file: Path | None = None              # served (a byte range of); Content-Type is in ``headers``
    start: int = 0
    length: int = 0
    #: Called once the response has been written (or dropped): unpins the cache file.
    release: Callable[[], None] | None = None

    def done(self) -> None:
        release, self.release = self.release, None
        if release is not None:
            release()


def _json(body: Any, status: int = 200, **headers: str) -> GameResponse:
    return GameResponse(status=status, body=body, headers=dict(headers))


def _error(exc: GameError) -> GameResponse:
    extra = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
    return _json({"error": exc.message, "code": exc.code}, exc.status, **extra)


def parse_range(header: str | None, size: int) -> tuple[int, int] | str | None:
    """``(start, end)`` inclusive for a satisfiable single range, ``"bad"`` when
    unsatisfiable (416), ``None`` to ignore (no/invalid/multi range, or
    ``bytes=10-5`` -> full 200 body)."""
    if not header:
        return None
    m = re.match(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", header)
    if not m or not (m.group(1) or m.group(2)):
        return None
    first, last = m.group(1), m.group(2)
    if first and last and int(last) < int(first):
        return None  # RFC 9110 14.1.2: last < first is an invalid spec -> ignore the header
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


@dataclass
class _Build:
    """One pool build's streaming state: depth, variants, the time budget and the
    results fetched so far (``memo``, kept on the pool when the build is cut short)."""

    depth: int
    variants: bool
    deadline: float | None = None
    memo: dict[tuple, list[Any]] = field(default_factory=dict)
    partial: bool = False
    allowed: frozenset[str] | None = None
    failed: list[str] = field(default_factory=list)   # permanent per-call failures (deleted playlist...)
    transient: bool = False                           # budget, back-off, rate limit, network, 5xx, auth

    def failure(self, gsrc: GameSource, what: str) -> None:
        """Record a failed streaming call (``gsrc._stream_call`` just returned ``None``)."""
        self.partial = True
        if getattr(gsrc._tls, "transient", True):
            self.transient = True
        else:
            self.failed.append(what)

    def run(self, gsrc: GameSource, memokey: tuple, service: str, call: Callable[[], list[Any]]) -> list[Any]:
        """``call()`` under the provider lock, once per build: a repeat of a key
        reuses its result; past the deadline or on a failure nothing is fetched / kept
        and the build is marked ``partial``."""
        if memokey in self.memo:
            return self.memo[memokey]
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.partial = self.transient = True
            return []
        got = gsrc._stream_call(service, call)
        if got is None:
            self.failure(gsrc, " ".join(str(k) for k in memokey))
            return []
        self.memo[memokey] = got
        return got


class GameSource:
    """Profiles, bags, now-playing and the WAV cache behind ``/api/game/*``."""

    def __init__(self, engine: Any, *, cache_dir: Path | None = None,
                 ffmpeg: Callable[[], str | None] = find_ffmpeg,
                 settings: Callable[[], Any] | None = None,
                 wait_s: float = DEFAULT_WAIT_S, history_path: Path | None = None) -> None:
        self.engine = engine
        self._ffmpeg = ffmpeg
        self._settings = settings or _load_settings
        self._tls = threading.local()      # one Settings load per request (see handle())
        self.rng_factory: Callable[[], random.Random] = random.Random  # tests seed this
        self.wait_s = wait_s
        if cache_dir is None:
            from harmony.config import cache_dir as _cache_dir

            cache_dir = _cache_dir() / "game-audio"
        self.cache = AudioCache(cache_dir, self._cap_bytes)
        self._hist = History(lambda: history_path or _history_path(),
                             lambda: bool(getattr(self._cfg(), "game_history_enabled", True)))
        self._profiles_lock = threading.Lock()
        self._pools: dict[tuple[str, str, str], TrackPool] = {}
        self._pools_lock = threading.Lock()
        self._meta: collections.OrderedDict[tuple[str, str], dict[str, Any]] = collections.OrderedDict()
        self._meta_lock = threading.Lock()
        self._bad_lock = threading.Lock()
        self._bo_lock = threading.Lock()
        self._bo: dict[str, dict[str, float]] = {}   # per-service back-off state
        self._bad: collections.OrderedDict[tuple[str, str], float] = collections.OrderedDict()
        self._now: dict[str, Any] | None = None
        self._history: collections.deque[dict[str, Any]] = collections.deque(maxlen=HISTORY_MAX)

    # -- settings ---------------------------------------------------------------

    def _cfg(self) -> Any:
        """The Settings for this request: loaded once by :meth:`handle`, not once
        per question asked of it (``enabled()``, ``_stored()``, the cache cap...)."""
        snap = getattr(self._tls, "settings", None)
        return snap if snap is not None else self._settings()

    def _cap_bytes(self) -> int:
        try:
            mb = int(getattr(self._cfg(), "game_cache_max_mb", DEFAULT_CACHE_MB))
        except (TypeError, ValueError):
            mb = DEFAULT_CACHE_MB
        return max(MIN_CACHE_MB, mb) * 1024 * 1024

    def _stream_depth(self) -> int:
        try:
            n = int(getattr(self._cfg(), "game_stream_search_limit", DEFAULT_STREAM_DEPTH))
        except (TypeError, ValueError):
            n = DEFAULT_STREAM_DEPTH
        return min(MAX_STREAM_DEPTH, max(MIN_STREAM_DEPTH, n))

    def enabled(self) -> bool:
        return bool(getattr(self._cfg(), "game_source_enabled", True))

    def _stored(self) -> dict[str, Any]:
        raw = getattr(self._cfg(), "game_profiles", None)
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
        stored = self._stored()
        for name in sorted(stored):
            try:
                out.append({"name": name, **validate_profile(stored[name])})
            except GameError as exc:
                log.warning("game profile %r is invalid: %s", name, exc.message)
        return out

    def put_profile(self, name: str, body: Any) -> dict[str, Any]:
        validate_name(name)
        profile = validate_profile(body, name)
        # The default container is not persisted, so a rollback to an older Harmony (which
        # rejects unknown format keys) still validates every stored profile.
        stored = {**profile, "format": {k: v for k, v in profile["format"].items()
                                        if k != "container" or v != "wav"}}
        from harmony.config import Settings

        with self._profiles_lock:
            s = Settings.load()
            profiles = dict(s.game_profiles) if isinstance(s.game_profiles, dict) else {}
            if name not in profiles and len(profiles) >= MAX_PROFILES:
                raise GameError(400, "too_many_profiles", f"at most {MAX_PROFILES} profiles")
            own = _size(stored)
            if own > MAX_PROFILE_BYTES:
                raise GameError(413, "profile_too_large",
                                f"profile is {own} bytes serialised; the limit is {MAX_PROFILE_BYTES}")
            others = sum(_size(v) for k, v in profiles.items() if k != name)
            if others + own > MAX_PROFILES_BYTES:
                raise GameError(413, "profiles_too_large",
                                f"all profiles together may take at most {MAX_PROFILES_BYTES} bytes "
                                f"serialised ({others + own} with this one); delete or shrink some")
            profiles[name] = stored
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
        self._hist.forget(name)

    def _drop_pools(self, name: str, ttype: str | None = None) -> None:
        with self._pools_lock:
            for k in [k for k in self._pools if k[0] == name
                      and (ttype is None or k[1].casefold() == ttype.casefold())]:
                del self._pools[k]

    def forget_history(self, name: str, ttype: str | None = None) -> dict[str, Any]:
        """Reset the no-repeat memory of a profile (or one of its types)."""
        validate_name(name)
        if ttype is not None and not TYPE_RE.match(ttype):
            raise GameError(400, "bad_request", '"type" must be a music type name such as "explore"')
        self._drop_pools(name, ttype)   # the live bags start over too (first, so nothing re-records)
        cleared = self._hist.forget(name, ttype)
        return {"ok": True, "profile": name, "type": ttype, "cleared": cleared}

    # -- candidate collection ------------------------------------------------------

    def _guard(self, service: str, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        """Call a provider method under its lock; a failing service is skipped."""
        try:
            with self.engine._plock(service):
                return fn(*args, **kw)
        except Exception as exc:  # noqa: BLE001 - one service failing must not sink the pick
            log.info("game source: %s call failed: %s", service, exc)
            return None

    def _stream_call(self, service: str, fn: Callable[..., Any], *args: Any, _exact: bool = False,
                     **kw: Any) -> Any:
        """:meth:`_guard` for streaming services plus a back-off: after a rate limit, or
        :data:`BACKOFF_FAILS` transient failures (network, timeout, 5xx, auth) in a row, the service gets
        no game-source requests for 30 s, doubling to 10 min; a success resets it. A failure
        of one call (a deleted playlist, a changed page layout) never backs the service off.
        ``None`` = failed or backing off; ``self._tls.transient`` then says whether the failure
        may pass (:func:`_transient`) or is permanent for that call."""
        self._tls.transient = True
        with self._bo_lock:
            st = self._bo.get(service)
            if st and time.monotonic() < st["until"]:
                return None
        try:
            with self.engine._plock(service):
                result = fn(*args, **kw)
        except Exception as exc:  # noqa: BLE001 - one service failing must not sink the pick
            log.info("game source: %s call failed: %s", service, exc)
            self._tls.transient = _transient(exc, _exact)
            if not self._tls.transient:
                return None
            with self._bo_lock:
                st = self._bo.setdefault(service, {"fails": 0.0, "until": 0.0, "delay": BACKOFF_MIN_S})
                st["fails"] += 1
                if isinstance(exc, RateLimitedError) or st["fails"] >= BACKOFF_FAILS:
                    st["until"] = time.monotonic() + st["delay"]
                    st["delay"] = min(st["delay"] * 2, BACKOFF_MAX_S)
                    st["fails"] = 0.0
                    log.info("game source: backing off %s", service)
            return None
        with self._bo_lock:
            self._bo.pop(service, None)
        return result

    def _excluded(self) -> set[str]:
        raw = getattr(self._cfg(), "game_excluded_services", None)
        names = {str(n).strip().lower() for n in raw} if isinstance(raw, (list, tuple, set)) else set()
        return {_SOURCE_SERVICE.get(n, n) for n in names}

    def _services(self, sources: str) -> frozenset[str]:
        """The services a ``sources`` value allows, less the server-wide exclusions."""
        base = set(_SOURCE_SERVICE.values()) if sources == "all" else \
            {_SOURCE_SERVICE[n] for n in sources.split(",")}
        return frozenset(base - self._excluded())

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

    def _stream_search(self, ctx: _Build, svc: str, prov: Any, query: str, depth: int) -> list[Any]:
        """``depth`` tracks for ``query`` on a streaming service: one call, or (Qobuz)
        pages of :data:`STREAM_PAGE` via ``offset``. ytmusicapi follows its own
        continuations up to ``limit``."""
        page = STREAM_PAGE.get(svc) or depth
        out: list[Any] = []
        while len(out) < depth:
            want = min(page, depth - len(out))
            extra = {"offset": len(out)} if out else {}
            got = ctx.run(self, (svc, "search", query, len(out)), svc, lambda w=want, x=extra: list(
                getattr(prov.search(query, kinds=("tracks",), limit=w, **x), "tracks", None) or []))
            out += got
            if len(got) < want:
                break
        return out

    def _collect_one(self, sel: dict[str, Any], allowed: frozenset[str], ctx: _Build) -> list[Any]:
        (kind, arg), = sel.items()
        lib = self._library() if "local" in allowed else None
        streaming = [(svc, prov) for svc, prov in self._streaming() if svc in allowed]
        tracks: list[Any] = []
        if kind == "genre":
            if lib is not None:
                page = self._guard("local", lib.browse_view, "songs", genre=arg, limit=GENRE_CAP,
                                   order="artist")
                tracks += (page or {}).get("songs") or []
            # "<genre> music" finds the genre's songs; a few variants widen the net
            queries = [(f"{arg} music", ctx.depth)]
            if ctx.variants:
                queries += [(q, max(MIN_STREAM_DEPTH, ctx.depth // 2))
                            for q in (f"best {arg} songs", f"{arg} playlist")]
            for query, depth in queries:
                for svc, prov in streaming:
                    tracks += self._stream_search(ctx, svc, prov, query, depth)
            if ctx.variants:  # YouTube Music's Moods & Genres playlists named like the genre
                for svc, prov in streaming:
                    mood = getattr(prov, "genre_playlist_tracks", None)
                    if mood is not None:
                        tracks += ctx.run(self, (svc, "mood", arg), svc, lambda m=mood: list(
                            m(arg, playlists=MOOD_PLAYLISTS, per_playlist=MOOD_TRACKS)))
        elif kind == "query":
            if lib is not None:
                r = self._guard("local", lib.search, arg, kinds=("tracks",), limit=LIB_SEARCH_LIMIT)
                tracks += list(getattr(r, "tracks", None) or [])
            for svc, prov in streaming:
                tracks += self._stream_search(ctx, svc, prov, arg, ctx.depth)
        else:  # playlist / album / artist: an exact service + id
            service, item_id = arg["service"], arg["id"]
            if service == "local":
                prov = lib
            elif service in allowed:
                prov = dict(streaming).get(service)
            else:
                log.info("game source: sources=%s skips %s selector for %s", ",".join(sorted(allowed)), kind, service)
                prov = None
            if prov is not None:
                fn = {"playlist": prov.get_playlist_tracks, "album": prov.get_album_tracks,
                      "artist": lambda i: prov.get_artist_top_tracks(i, limit=ARTIST_TRACKS)}[kind]
                got = (self._guard(service, fn, item_id) if service == "local"
                       else self._stream_call(service, fn, item_id, _exact=True))
                if got is None and service != "local":
                    ctx.failure(self, f"{kind} {service}:{item_id}")
                tracks += got or []
        return tracks

    def _collect(self, selectors: list[dict[str, Any]], sources: str,
                 ctx: _Build | None = None) -> dict[tuple[str, str], dict[str, Any]]:
        ctx = ctx or _Build(depth=self._stream_depth(), variants=False)
        allowed = ctx.allowed if ctx.allowed is not None else self._services(sources)
        by_song: dict[str, dict[str, Any]] = {}
        order: list[dict[str, Any]] = []
        for sel in selectors:
            for t in self._collect_one(sel, allowed, ctx):
                ref = _ref(t)
                if ref is None or ref["service"] not in allowed:
                    continue
                dur = ref["duration_s"]
                if ref["service"] != "local" and isinstance(dur, (int, float)) and dur \
                        and not STREAM_MIN_S <= dur <= STREAM_MAX_S:
                    continue
                # One song once, even if the Library and a service (or two query variants)
                # both have it: the Library copy wins, else the first seen, so a "repeat" can't hide as a twin.
                song = _song_key(ref["title"], ref["artist"])
                if song and song in by_song:
                    kept = by_song[song]
                    if _SERVICE_RANK.get(ref["service"], 1) < _SERVICE_RANK.get(kept["service"], 1):
                        kept.update(ref)
                    continue
                if song:
                    by_song[song] = ref
                order.append(ref)
        return {(r["service"], r["id"]): r for r in order}

    def _stream_pool_ttl(self) -> float:
        try:
            n = float(getattr(self._cfg(), "game_stream_pool_ttl_s", DEFAULT_STREAM_POOL_TTL_S))
        except (TypeError, ValueError):
            n = DEFAULT_STREAM_POOL_TTL_S
        return min(MAX_STREAM_POOL_TTL_S, max(MIN_STREAM_POOL_TTL_S, n))

    @staticmethod
    def _has_streaming(selectors: list[dict[str, Any]], allowed: frozenset[str]) -> bool:
        stream = allowed - {"local"}
        for sel in selectors:
            (kind, arg), = sel.items()
            if stream and (kind in ("genre", "query") or arg["service"] in stream):
                return True
        return False

    def _new_build(self, pool: TrackPool, fp: str, allowed: frozenset[str]) -> _Build:
        fresh = pool.memo_fp == fp and time.monotonic() - pool.memo_at <= POOL_TTL_S
        return _Build(depth=self._stream_depth(),
                      variants=bool(getattr(self._cfg(), "game_genre_variants", True)),
                      memo=pool.memo if fresh else {}, allowed=allowed)

    def _apply(self, pool: TrackPool, key: tuple[str, str, str], fp: str,
               refs: dict[tuple[str, str], dict[str, Any]], ctx: _Build, ttl: float, streaming: bool) -> None:
        """Swap a finished build into ``pool`` (the caller holds ``pool.lock``)."""
        now = time.monotonic()
        same = fp == pool.fingerprint
        # Builds whose only failures are permanent per-call ones (a deleted playlist...) count
        # towards giving up; transient partials (outage, rate limit, back-off, budget) do not.
        permanent = ctx.partial and not ctx.transient
        attempts = pool.attempts + 1 if permanent else pool.attempts if ctx.partial else 0
        # After MAX_PARTIAL_ATTEMPTS of those in a row: accept the result and use the normal TTL.
        giveup = permanent and attempts >= MAX_PARTIAL_ATTEMPTS
        if giveup:
            log.warning("game source: pool %s still incomplete after %d builds (%s); using it as is",
                        "/".join(key), attempts, ", ".join(ctx.failed) or "time budget")
        # A partial rebuild never shrinks a pool we already have.
        if refs and ctx.partial and same and len(refs) < len(pool.refs) and not giveup:
            refs = {}
        got = bool(refs)
        if refs or not same:
            pool.refs = refs
            pool.bag.sync(list(refs), self._hist.served("/".join(key), fp))
            pool.fingerprint = fp
        # The memo of fetched results lives across consecutive partial builds (so each
        # one finishes what the last could not), but not forever: it is dropped after
        # MAX_PARTIAL_ATTEMPTS builds or POOL_TTL_S, so a call that keeps failing cannot
        # freeze the successful ones.
        pool.attempts = 0 if giveup else attempts
        if ctx.partial and not giveup:
            if ctx.memo is not pool.memo:   # a fresh memo (new, or the old one had expired)
                pool.memo_at = now
            pool.memo, pool.memo_fp = ctx.memo, fp
        else:
            pool.memo, pool.memo_fp = {}, ""
        # An empty refresh of an unchanged selector keeps the old pool (a service blip
        # must not silence the game's music). Retries: a partial list after a minute,
        # an empty STREAMING list after 30 s, an empty library list (mid-scan) after a few seconds.
        if ctx.partial and not giveup:
            retry = PARTIAL_POOL_RETRY_S
        elif not got:
            retry = STREAM_EMPTY_RETRY_S if streaming else EMPTY_POOL_RETRY_S
        else:
            retry = ttl
        pool.expires = now + retry

    def _refresh_async(self, pool: TrackPool, key: tuple[str, str, str], fp: str,
                       selectors: list[dict[str, Any]], sources: str, ttl: float,
                       allowed: frozenset[str]) -> None:
        """Rebuild an already-populated streaming pool in ONE daemon thread (the caller holds
        ``pool.lock``); ``next`` keeps serving the old candidates meanwhile."""
        if pool.refreshing:
            return
        pool.refreshing = True
        ctx = self._new_build(pool, fp, allowed)

        def run() -> None:
            try:
                ctx.deadline = time.monotonic() + STREAM_BUDGET_S
                refs = self._collect(selectors, sources, ctx)
                with pool.lock:
                    if pool.fingerprint == fp:
                        self._apply(pool, key, fp, refs, ctx, ttl, True)
            except Exception as exc:  # noqa: BLE001 - never leave the pool without a retry
                log.warning("game source: background pool refresh failed: %s", exc)
                with pool.lock:
                    pool.expires = time.monotonic() + PARTIAL_POOL_RETRY_S
            finally:
                with pool.lock:
                    pool.refreshing = False

        pool.worker = threading.Thread(target=run, daemon=True, name="game-pool-refresh")
        pool.worker.start()

    def _pool(self, name: str, ttype: str, sources: str, selectors: list[dict[str, Any]]) -> TrackPool:
        key = (name, ttype, sources)
        with self._pools_lock:
            pool = self._pools.get(key)
            if pool is None:
                pool = self._pools[key] = TrackPool(self.rng_factory())
        allowed = self._services(sources)
        fp = hashlib.sha256(json.dumps([selectors, sources, sorted(allowed)], sort_keys=True).encode()).hexdigest()
        streaming = self._has_streaming(selectors, allowed)
        ttl = self._stream_pool_ttl() if streaming else POOL_TTL_S
        with pool.lock:
            same = fp == pool.fingerprint
            if same and time.monotonic() < pool.expires:
                return pool
            if same and streaming and pool.refs:   # stale-while-revalidate
                self._refresh_async(pool, key, fp, selectors, sources, ttl, allowed)
                return pool
            ctx = self._new_build(pool, fp, allowed)   # a cold build (or a library pool) is synchronous
            ctx.deadline = time.monotonic() + STREAM_BUDGET_S
            self._apply(pool, key, fp, self._collect(selectors, sources, ctx), ctx, ttl, streaming)
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
            now = time.monotonic()
            with self._bad_lock:
                self._bad.pop((service, item_id), None)
                self._bad[(service, item_id)] = now
                while self._bad and (len(self._bad) > MAX_BAD
                                     or now - next(iter(self._bad.values())) > BAD_TTL_S):
                    self._bad.popitem(last=False)

    def _is_bad(self, key: tuple[str, str]) -> bool:
        with self._bad_lock:
            at = self._bad.get(key)
            if at is None:
                return False
            if time.monotonic() - at > BAD_TTL_S:
                self._bad.pop(key, None)
                return False
            return True

    def _take(self, pool: TrackPool, count: int, consume: bool) -> list[dict[str, Any]]:
        """The next ``count`` playable candidates, skipping tracks that recently
        failed to resolve (region/age-blocked on YouTube...). Peek and consume are
        the same :meth:`Bag.pick` walk (consume just pops what it walked), so a
        peek always equals the next consuming call; you get ``count`` tracks
        whenever at least one is playable, never the same one twice in a row
        unless it is the only playable one."""
        good = sum(1 for k in pool.refs if not self._is_bad(k))
        if not good:
            return []
        keys = pool.bag.pick(count, lambda k: k in pool.refs and not self._is_bad(k),
                             repeat_ok=good == 1, consume=consume)
        return [pool.refs[k] for k in keys]

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
        ext = f.get("container", "wav")
        return (f"/api/game/audio/{service}/{quote(item_id, safe='')}.{ext}?rate={f['rate']}"
                f"&bits={f['bits']}&channels={f['channels']}"
                f"&normalize={int(profile['normalize'])}&trim={int(profile['trim_silence'])}")

    def _publish(self, refs: list[dict[str, Any]], sources: str, profile: dict[str, Any]) -> list[dict[str, Any]]:
        """Candidate records -> API tracks (streaming ones swapped for the
        Library's copy when the engine has one, per the library-preferred setting)."""
        swap: dict[int, Any] = {}
        if "local" in self._services(sources) and self.engine._library_preferred():
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
        unknown = set(body) - {"profile", "type", "sources", "count", "peek", "container"}
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
        if "container" in body:  # the request overrides the profile's format.container
            container = _container_arg(body["container"])
            profile = {**profile, "format": {**profile["format"], "container": container}}
        try:
            sources = normalize_sources(body.get("sources", profile["sources"]), "sources")
        except GameError as exc:
            raise GameError(400, "bad_request", exc.message) from exc
        ttype = self._resolve_type(profile, requested)
        selectors = selectors_of(profile["types"][ttype])
        pool = self._pool(name, ttype, sources, selectors)
        with pool.lock:
            refs = self._take(pool, count, consume=not peek)
            size = len(pool.refs)
            if refs and not peek:   # a peek never touches the no-repeat memory
                self._hist.record(f"{name}/{ttype}/{sources}", pool.fingerprint,
                                  [(r["service"], r["id"]) for r in refs])
        self._hist.flush()
        if not refs:
            hint = (f" (sources={sources}: is the Library enabled and does it hold matching music?)"
                    if sources == "library" else f" (sources={sources}; services excluded or signed out are skipped)"
                    if sources != "all" else "")
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
        return {"enabled": self.enabled(), "ffmpeg": bool(self._ffmpeg()), "containers": list(CONTAINERS),
                "cache": self.cache.stats(), "now_playing": self._now_view(),
                "history": [{k: h.get(k) for k in ("profile", "type", "service", "id", "title",
                                                   "artist", "started_at")}
                            for h in list(self._history)[:20]],
                "profiles": sorted(self._stored()),
                "services": self._usable_services(),
                "excluded_services": sorted(n for n in SOURCE_NAMES if _SOURCE_SERVICE[n] in self._excluded())}

    def _usable_services(self) -> dict[str, bool]:
        """Which sources a ``sources`` of "all" would use right now."""
        excluded = self._excluded()
        out = {"library": False, "ytmusic": False, "qobuz": False}
        lib = self._library() if "local" not in excluded else None
        if lib is not None:
            page = self._guard("local", lib.browse_view, "songs", limit=1)
            out["library"] = bool((page or {}).get("total") or (page or {}).get("songs"))
        live = {svc for svc, _p in self._streaming()}
        out["ytmusic"] = "ytmusic" in live and "ytmusic" not in excluded
        out["qobuz"] = "qobuz" in live and "qobuz" not in excluded
        return out

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
        container = next((c for c in CONTAINERS if filename.endswith(f".{c}")), None)
        if container is None:
            raise GameError(404, "not_found", "audio is served as <id>.wav or <id>.flac")
        service, item_id = _check_track_ref(service, filename[: -len(container) - 1])
        rate, bits, channels, normalize, trim = self._parse_format(query)
        wait = self.wait_s
        if query.get("wait"):
            try:
                wait = min(MAX_WAIT_S, max(0.0, float(query["wait"][0])))
            except ValueError as exc:
                raise GameError(400, "bad_format", "wait must be a number of seconds") from exc
        ffmpeg = self._ffmpeg()
        # ``|flac`` only for FLAC: the WAV key stays what it always was (cached files stay valid)
        fmt = f"{rate}|{bits}|{channels}|{int(normalize)}|{int(trim)}" + ("|flac" if container == "flac" else "")

        def key_of(svc: str, tid: str) -> str:
            h = hashlib.sha256(f"{_PIPELINE}|{svc}|{tid}|{fmt}".encode()).hexdigest()[:40]
            return h + ".flac" if container == "flac" else h

        # The cache is keyed by the source that will actually be transcoded, not by
        # the id asked for: asking for a streaming id whose Library copy is known
        # means the Library file (a better, offline source) and must not be mixed up
        # with a cached stream of the same song (nor with it once the copy is gone).
        psvc, pid = self._predict_source(service, item_id)
        key = key_of(psvc, pid)
        if ffmpeg is None and not self.cache.path_for(key).exists():
            raise GameError(503, "ffmpeg_missing", "ffmpeg is not installed on this Harmony server")

        def build(tmp: Path) -> str:
            assert ffmpeg is not None
            try:
                svc, tid, source = self.engine._resolve_preferring_library(service, item_id)
            except KeyError as exc:
                raise GameError(404, "unknown_service", f"service {exc} is not available") from exc
            except GameError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_bad(service, item_id)  # next() stops offering it for a while
                raise GameError(502, "source_unavailable", _scrub(f"could not resolve the track: {exc}")) from exc
            (transcode_to_flac if container == "flac" else transcode_to_wav)(
                ffmpeg, ffmpeg_input_args(source.url, source.headers), tmp, rate=rate,
                bits=bits, channels=channels, normalize=normalize, trim=trim)
            return key_of(svc, tid)  # stored under what was really used

        path: Path | None = None
        size = 0
        for _attempt in range(2):  # a file evicted/replaced under us once -> rebuild once
            path = self.cache.get(key, build, wait)
            if path is None:
                return _json({"status": "transcoding", "retry_after_s": RETRY_AFTER_S}, 202,
                             **{"Retry-After": str(RETRY_AFTER_S)})
            try:
                size = path.stat().st_size
                break
            except OSError:
                self.cache.unpin(path)
                path = None
        if path is None:
            raise GameError(503, "cache_busy", "the audio cache entry vanished; retry shortly",
                            retry_after=RETRY_AFTER_S)
        release = lambda p=path: self.cache.unpin(p)  # noqa: E731
        headers = {"Content-Type": f"audio/{container}", "Accept-Ranges": "bytes", "ETag": f'"{path.stem}"',
                   "Cache-Control": "private, max-age=3600"}
        rng = parse_range(range_header, size)
        if rng == "bad":
            release()
            return GameResponse(416, None, {**headers, "Content-Range": f"bytes */{size}",
                                            "Content-Length": "0"})
        if isinstance(rng, tuple):
            start, end = rng
            return GameResponse(206, None, {**headers, "Content-Range": f"bytes {start}-{end}/{size}",
                                            "Content-Length": str(end - start + 1)},
                                file=path, start=start, length=end - start + 1, release=release)
        return GameResponse(200, None, {**headers, "Content-Length": str(size)},
                            file=path, start=0, length=size, release=release)

    def _predict_source(self, service: str, item_id: str) -> tuple[str, str]:
        """The (service, id) the engine will transcode for a request: the Library
        copy when one is already matched (``_prefer_library``, a lookup -- no
        network), else the id itself. Engines without it just use the id."""
        prefer = getattr(self.engine, "_prefer_library", None)
        if prefer is None:
            return service, item_id
        try:
            svc, tid = prefer(service, item_id)
            return str(svc), str(tid)
        except Exception:  # noqa: BLE001 - a prediction only picks the cache slot
            return service, item_id

    # -- dispatch ---------------------------------------------------------------------------

    def handle(self, method: str, parts: list[str], query: Mapping[str, list[str]], body: Any,
               headers: Mapping[str, str] | Any) -> GameResponse:
        """Route ``method`` + decoded path segments (``["api", "game", ...]``).
        The caller has already enforced the personal-key gate."""
        try:
            self._tls.settings = self._settings()
            return self._route(method, parts[2:], query, body, headers)
        except GameError as exc:
            return _error(exc)
        except Exception as exc:  # noqa: BLE001 - JSON, not a dropped connection
            log.warning("game source error on %s /%s: %s", method, "/".join(parts), exc)
            return _json({"error": _scrub(str(exc)) or "internal error", "code": "internal"}, 500)
        finally:
            self._tls.settings = None

    def _route(self, method: str, p: list[str], query: Mapping[str, list[str]], body: Any,
               headers: Any) -> GameResponse:
        head = method == "HEAD"
        if method == "GET" and p == ["status"]:
            return _json(self.status())
        if p and p[0] in ("profiles", "next", "playing", "audio", "history") and not self.enabled():
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
        if p == ["history"]:
            if method == "DELETE":
                prof, typ = query.get("profile"), query.get("type")
                if not prof:
                    raise GameError(400, "bad_request", "pass ?profile=<name>[&type=<music type>]")
                return _json(self.forget_history(prof[0], typ[0] if typ else None))
            raise GameError(405, "method_not_allowed", "use DELETE")
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
                    resp.done()
                return resp
            raise GameError(405, "method_not_allowed", "use GET or HEAD")
        raise GameError(404, "not_found", "unknown game API route")


def _container_arg(value: Any) -> str:
    if not isinstance(value, str) or value not in CONTAINERS:
        raise GameError(400, "bad_request", '"container" must be "wav" or "flac"')
    return value


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _check_track_ref(service: Any, item_id: Any) -> tuple[str, str]:
    if service not in SERVICES:
        raise GameError(400, "bad_request", f"service must be one of {', '.join(SERVICES)}")
    if isinstance(item_id, int) and not isinstance(item_id, bool):
        item_id = str(item_id)
    if not isinstance(item_id, str) or not ID_RE.match(item_id) or ".." in item_id:
        raise GameError(400, "bad_request", "invalid track id")
    return service, item_id


def _history_path() -> Path:
    from harmony.config import settings_path

    return settings_path().parent / HISTORY_FILE


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
    Content-Length; audio streams the file range from disk. Always releases the
    response's cache pin when finished."""
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
        fh = None
        if not head:
            try:  # open BEFORE the headers: a vanished file is a clean 503, not a torn response
                fh = open(resp.file, "rb")  # noqa: SIM115 - closed in the finally below
            except OSError:
                fail = _error(GameError(503, "cache_busy", "the audio cache entry vanished; retry shortly",
                                        retry_after=RETRY_AFTER_S))
                resp.done()
                send_response(handler, fail)
                return
        try:
            handler.send_response(resp.status)
            for k, v in resp.headers.items():
                handler.send_header(k, v)
            handler.end_headers()
            if fh is None:
                return
            fh.seek(resp.start)
            left = resp.length
            while left > 0:
                chunk = fh.read(min(256 * 1024, left))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                left -= len(chunk)
        finally:
            if fh is not None:
                fh.close()
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True  # the game dropped the download; nothing to do
    finally:
        resp.done()
