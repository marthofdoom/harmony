"""Local music library — index the instance's own music folders and serve them.

This is the far end of the Lidarr loop: Lidarr acquires an album and files it
into a music folder; Harmony indexes that folder and serves it to every client
as the ``local`` service (search, album/artist pages, queue, stream, cast).

* :class:`LibraryIndex` — a SQLite index of audio files (path, size, mtime and
  the tags that matter: title/artist/album/album-artist/track/disc/year/ISRC/
  MusicBrainz ids/duration). Scans are incremental (unchanged files are skipped
  by size+mtime) and can be scoped to a sub-path, which is what Lidarr's import
  webhook triggers.
* Tags come from ``mutagen`` when it's installed (the ``library``/``server``
  extras); without it the scanner falls back to the folder/file naming Lidarr
  writes (``Artist/Album (Year)/Artist - Album - 01 - Title.flac``), so a
  minimal install still indexes a Lidarr library sensibly.
* Artwork is served through a *capability* URL (``/art/<album>.<sig>``): an HMAC
  of the album id under a per-instance secret. It needs no personal key — an
  ``<img>``, the Android app and a cast device can all fetch it — yet can't be
  enumerated without the secret.
* :func:`map_path` applies Lidarr "remote path mappings" so paths Lidarr reports
  (from inside its container) resolve on this machine.

Engine-layer, gi-free.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Audio extensions indexed, with the MIME type the stream is served as.
AUDIO_TYPES: dict[str, str] = {
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".wv": "audio/x-wavpack",
    ".ape": "audio/x-ape",
}

#: Folder images tried (case-insensitively), best first.
_COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "albumartsmall")
_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id              TEXT PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,
    size            INTEGER NOT NULL,
    mtime           REAL NOT NULL,
    title           TEXT NOT NULL,
    artist          TEXT NOT NULL,
    artist_id       TEXT NOT NULL,
    album           TEXT NOT NULL,
    album_artist    TEXT NOT NULL,
    album_artist_id TEXT NOT NULL,
    album_id        TEXT NOT NULL,
    track_no        INTEGER,
    disc_no         INTEGER,
    year            INTEGER,
    date            TEXT,
    duration_s      INTEGER,
    isrc            TEXT,
    mb_releasegroup TEXT,
    mb_release      TEXT,
    mb_artist       TEXT,
    mime            TEXT NOT NULL,
    haystack        TEXT NOT NULL,
    added_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tracks_album ON tracks(album_id);
CREATE INDEX IF NOT EXISTS tracks_album_artist ON tracks(album_artist_id);
CREATE INDEX IF NOT EXISTS tracks_artist ON tracks(artist_id);
CREATE INDEX IF NOT EXISTS tracks_rg ON tracks(mb_releasegroup);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------
# Normalisation + ids
# --------------------------------------------------------------------------


def norm(text: str | None) -> str:
    """Casefolded, accent-stripped, whitespace-collapsed text for matching."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", stripped.casefold()).split())


def _hid(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


def track_id_for(path: str | Path) -> str:
    return _hid("t", str(path))


def artist_id_for(name: str) -> str:
    return _hid("a", norm(name) or "unknown artist")


def album_id_for(album_artist: str, album: str) -> str:
    return _hid("al", norm(album_artist) or "unknown artist", norm(album) or "unknown album")


# --------------------------------------------------------------------------
# Lidarr remote path mapping
# --------------------------------------------------------------------------


def map_path(path: str, mappings: Iterable[dict[str, str]] | None) -> str:
    """Rewrite a path as Lidarr sees it into the same path on this machine.

    ``mappings`` is ``[{"remote": "/music", "local": "/srv/media/music"}, ...]``;
    the longest matching ``remote`` prefix wins, matched on whole path segments.
    Unmapped paths come back unchanged.
    """
    if not path:
        return path
    best: tuple[str, str] | None = None
    for m in mappings or ():
        remote = (m.get("remote") or "").rstrip("/\\")
        local = (m.get("local") or "").rstrip("/\\")
        if not remote:
            continue
        if path == remote or path.startswith(remote + "/") or path.startswith(remote + "\\"):
            if best is None or len(remote) > len(best[0]):
                best = (remote, local)
    if best is None:
        return path
    rest = path[len(best[0]):].replace("\\", "/")
    return (best[1] + rest) or "/"


# --------------------------------------------------------------------------
# Tag reading
# --------------------------------------------------------------------------


@dataclass(slots=True)
class FileTags:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track_no: int | None = None
    disc_no: int | None = None
    year: int | None = None
    date: str | None = None
    duration_s: int | None = None
    isrc: str | None = None
    mb_releasegroup: str | None = None
    mb_release: str | None = None
    mb_artist: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _first(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        value = value[0] if value else ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value).strip()


def _num(value: Any) -> int | None:
    """``"3"``, ``"3/12"``, ``(3, 12)`` → 3."""
    if isinstance(value, list | tuple) and value and isinstance(value[0], tuple):
        value = value[0]
    if isinstance(value, tuple):
        value = value[0] if value else None
    text = _first(value)
    m = re.match(r"\s*(\d+)", text)
    return int(m.group(1)) if m else None


def _year(date: str) -> int | None:
    m = re.search(r"(\d{4})", date or "")
    return int(m.group(1)) if m else None


# Easy-key → raw ID3 frame, for containers whose ID3 tag mutagen's "easy" mode
# doesn't wrap (WAV, AIFF): those come back as plain ``ID3`` frame maps.
_ID3_FRAMES = {
    "title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
    "tracknumber": "TRCK", "discnumber": "TPOS", "date": "TDRC", "originaldate": "TDOR",
    "year": "TYER", "isrc": "TSRC",
    "musicbrainz_releasegroupid": "TXXX:MusicBrainz Release Group Id",
    "musicbrainz_albumid": "TXXX:MusicBrainz Album Id",
    "musicbrainz_albumartistid": "TXXX:MusicBrainz Album Artist Id",
    "musicbrainz_artistid": "TXXX:MusicBrainz Artist Id",
}


def _mutagen_tags(path: Path) -> FileTags | None:
    """Tags via mutagen, or ``None`` when mutagen is absent/can't parse the file."""
    try:
        import mutagen
    except ImportError:
        return None
    try:
        f = mutagen.File(str(path), easy=True)
    except Exception as exc:  # noqa: BLE001 - a corrupt file mustn't stop a scan
        log.info("library: couldn't read tags from %s: %s", path, exc)
        return None
    if f is None:
        return None
    tags = f.tags or {}
    try:
        from mutagen.id3 import ID3

        raw_id3 = isinstance(tags, ID3)
    except ImportError:  # pragma: no cover - part of mutagen
        raw_id3 = False

    def get(*keys: str) -> str:
        for k in keys:
            try:
                if raw_id3:
                    frame = tags.get(_ID3_FRAMES.get(k, ""))
                    v = [str(t) for t in frame.text] if frame is not None else None
                else:
                    v = tags.get(k)
            except Exception:  # noqa: BLE001 - some tag maps raise on unknown keys
                v = None
            if v:
                return _first(v)
        return ""

    out = FileTags(
        title=get("title"),
        artist=get("artist"),
        album=get("album"),
        album_artist=get("albumartist", "album artist", "album_artist"),
        track_no=_num(get("tracknumber")),
        disc_no=_num(get("discnumber")),
        date=get("date", "originaldate", "year") or None,
        isrc=get("isrc") or None,
        mb_releasegroup=get("musicbrainz_releasegroupid") or None,
        mb_release=get("musicbrainz_albumid") or None,
        mb_artist=get("musicbrainz_albumartistid", "musicbrainz_artistid") or None,
    )
    out.year = _year(out.date or "")
    info = getattr(f, "info", None)
    length = getattr(info, "length", None)
    if length:
        out.duration_s = int(round(length))
    return out


_LEADING_NUM_RE = re.compile(r"^(?:(\d)[-.])?(\d{1,3})\s*[-._ ]\s*(.+)$")
_ALBUM_YEAR_RE = re.compile(r"^(.*?)\s*[(\[](\d{4})[)\]]\s*$")


def _path_tags(path: Path) -> FileTags:
    """Best-effort tags from the folder layout Lidarr (and most rippers) write:
    ``<root>/<Artist>/<Album> (<Year>)/[<Artist> - <Album> - ]<NN> - <Title>.ext``."""
    stem = path.stem
    title, track_no, disc_no = stem, None, None
    parts = [p.strip() for p in stem.split(" - ")]
    # "<Artist> - <Album> - 01 - Title" / "01 - Title": the last numeric part is
    # the track number, everything after it the title.
    for i in range(len(parts) - 1, -1, -1):
        if re.fullmatch(r"\d{1,3}", parts[i]) and i < len(parts) - 1:
            track_no = int(parts[i])
            title = " - ".join(parts[i + 1:])
            break
    else:
        m = _LEADING_NUM_RE.match(stem)
        if m:
            disc_no = int(m.group(1)) if m.group(1) else None
            track_no = int(m.group(2))
            title = m.group(3).strip()
    album_dir = path.parent.name
    artist_dir = path.parent.parent.name if path.parent.parent != path.parent else ""
    # A "CD1"/"Disc 2" folder sits between the album and the files.
    if re.fullmatch(r"(?i)(cd|disc|disk)\s*\d+", album_dir):
        dm = re.search(r"\d+", album_dir)
        disc_no = disc_no or (int(dm.group(0)) if dm else None)
        album_dir = path.parent.parent.name
        artist_dir = path.parent.parent.parent.name
    year = None
    m = _ALBUM_YEAR_RE.match(album_dir)
    if m:
        album_dir, year = m.group(1).strip(), int(m.group(2))
    return FileTags(title=title, artist=artist_dir, album=album_dir, album_artist=artist_dir,
                    track_no=track_no, disc_no=disc_no, year=year,
                    date=str(year) if year else None)


def read_tags(path: Path) -> FileTags:
    """Tags for one audio file: mutagen's, with the path layout filling gaps."""
    fallback = _path_tags(path)
    tags = _mutagen_tags(path)
    if tags is None:
        return fallback
    for name in ("title", "artist", "album", "track_no", "disc_no", "year", "date"):
        if not getattr(tags, name):
            setattr(tags, name, getattr(fallback, name))
    if not tags.album_artist:
        # No album-artist tag. When the file sits in an <Artist>/<Album> folder
        # for this very album, the folder names the album artist — that keeps a
        # compilation ("Various Artists/Now 80/…", a different artist per
        # track) one album instead of one album per track artist.
        if fallback.album_artist and norm(fallback.album) == norm(tags.album):
            tags.album_artist = fallback.album_artist
        else:
            tags.album_artist = tags.artist or fallback.album_artist
    return tags


def resolve(path: str | Path) -> Path:
    """``path`` made absolute with ``..``/symlinks resolved (no I/O failure)."""
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def within(path: str | Path, roots: Iterable[str | Path]) -> bool:
    """True when ``path`` really is (inside) one of ``roots`` — compared after
    resolving ``..`` and symlinks, so ``/music/../etc`` is NOT inside ``/music``."""
    p = resolve(path)
    return any(p == r or p.is_relative_to(r) for r in (resolve(x) for x in roots))


def has_mutagen() -> bool:
    try:
        import mutagen  # noqa: F401
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------------
# Artwork
# --------------------------------------------------------------------------


def folder_cover(directory: Path) -> Path | None:
    """The best cover image in an album folder (cover.jpg, folder.png, …)."""
    try:
        entries = [p for p in directory.iterdir() if p.is_file()]
    except OSError:
        return None
    images = {p.stem.lower(): p for p in entries if p.suffix.lower() in _IMAGE_TYPES}
    for name in _COVER_NAMES:
        if name in images:
            return images[name]
    return next(iter(sorted(images.values())), None) if images else None


def embedded_cover(path: Path) -> tuple[bytes, str] | None:
    """The front-cover picture embedded in an audio file (FLAC/ID3/MP4/Vorbis)."""
    try:
        import mutagen
    except ImportError:
        return None
    try:
        f = mutagen.File(str(path))
    except Exception:  # noqa: BLE001
        return None
    if f is None:
        return None
    pictures = getattr(f, "pictures", None)  # FLAC
    if pictures:
        pic = next((p for p in pictures if getattr(p, "type", 0) == 3), pictures[0])
        return bytes(pic.data), pic.mime or "image/jpeg"
    tags = getattr(f, "tags", None)
    if tags is None:
        return None
    try:
        for key in list(tags.keys()):
            if str(key).startswith("APIC"):  # ID3
                frame = tags[key]
                return bytes(frame.data), frame.mime or "image/jpeg"
        covr = tags.get("covr") if hasattr(tags, "get") else None  # MP4
        if covr:
            data = bytes(covr[0])
            fmt = getattr(covr[0], "imageformat", 13)
            return data, "image/png" if fmt == 14 else "image/jpeg"
        blocks = tags.get("metadata_block_picture") if hasattr(tags, "get") else None  # Vorbis
        if blocks:
            import base64

            from mutagen.flac import Picture

            pic = Picture(base64.b64decode(blocks[0]))
            return bytes(pic.data), pic.mime or "image/jpeg"
    except Exception:  # noqa: BLE001 - artwork is best-effort
        return None
    return None


class ArtSigner:
    """Signs album ids into unguessable, key-free artwork paths (``/art/<id>.<sig>``)."""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    @classmethod
    def for_dir(cls, directory: Path) -> ArtSigner:
        path = directory / "library-art.key"
        try:
            secret = path.read_bytes()
        except OSError:
            secret = b""
        if len(secret) < 16:
            secret = secrets.token_bytes(32)
            try:
                directory.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, secret)
                finally:
                    os.close(fd)
            except OSError as exc:  # unwritable: URLs just won't survive a restart
                log.warning("library: couldn't persist the artwork key (%s)", exc)
        return cls(secret)

    def sign(self, album_id: str) -> str:
        return hmac.new(self._secret, album_id.encode(), hashlib.sha256).hexdigest()[:20]

    def path_for(self, album_id: str) -> str:
        return f"/art/{album_id}.{self.sign(album_id)}"

    def verify(self, token: str) -> str | None:
        """``"<album_id>.<sig>"`` → the album id when the signature is valid."""
        album_id, _, sig = (token or "").rpartition(".")
        if not album_id or not sig:
            return None
        return album_id if hmac.compare_digest(sig, self.sign(album_id)) else None


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ScanReport:
    scanned: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"scanned": self.scanned, "added": self.added, "updated": self.updated,
                "removed": self.removed, "errors": self.errors,
                "seconds": round(self.seconds, 2)}


def iter_audio_files(root: Path) -> Iterator[Path]:
    """Every audio file under ``root`` (or ``root`` itself), skipping dot-dirs."""
    if root.is_file():
        if root.suffix.lower() in AUDIO_TYPES:
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in AUDIO_TYPES:
                yield Path(dirpath) / name


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}  # noqa: SIM118 - sqlite3.Row has no items()


class LibraryIndex:
    """Thread-safe SQLite index of the audio files under the library folders."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- meta -------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                               (key, value))
            self._conn.commit()

    # -- scanning ---------------------------------------------------------

    def _upsert(self, path: Path, st: os.stat_result, existing: dict[str, Any] | None) -> None:
        tags = read_tags(path)
        title = tags.title or path.stem
        artist = tags.artist or tags.album_artist or "Unknown Artist"
        album_artist = tags.album_artist or artist
        album = tags.album or path.parent.name or "Unknown Album"
        mime = AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream")
        haystack = " ".join(filter(None, (norm(title), norm(artist), norm(album),
                                          norm(album_artist))))
        row = {
            "id": track_id_for(path), "path": str(path), "size": st.st_size,
            "mtime": st.st_mtime, "title": title, "artist": artist,
            "artist_id": artist_id_for(artist), "album": album,
            "album_artist": album_artist, "album_artist_id": artist_id_for(album_artist),
            "album_id": album_id_for(album_artist, album), "track_no": tags.track_no,
            "disc_no": tags.disc_no, "year": tags.year, "date": tags.date,
            "duration_s": tags.duration_s, "isrc": tags.isrc,
            "mb_releasegroup": tags.mb_releasegroup, "mb_release": tags.mb_release,
            "mb_artist": tags.mb_artist, "mime": mime, "haystack": haystack,
            "added_at": existing["added_at"] if existing else self._added_at(path),
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            self._conn.execute(f"INSERT OR REPLACE INTO tracks ({cols}) VALUES ({marks})",
                               tuple(row.values()))

    def _added_at(self, path: Path) -> float:
        """Keep a row's first-seen time even when another scan wrote it meanwhile."""
        with self._lock:
            row = self._conn.execute("SELECT added_at FROM tracks WHERE path = ?",
                                     (str(path),)).fetchone()
        return row["added_at"] if row else time.time()

    def scan(self, roots: Iterable[str | Path], *, prune: bool = True,
             prune_missing: bool = True) -> ScanReport:
        """Index every audio file under ``roots`` (a folder, a sub-folder, or one
        file). Unchanged files (same size + mtime) are skipped. With ``prune``,
        rows under a scanned root whose file is gone are removed — so a rescan of
        one album folder (what the Lidarr webhook does) also catches deletions.

        A root that doesn't exist at all is dropped from the index only with
        ``prune_missing`` — right for a deleted album folder, wrong for a library
        folder on a share that's merely unmounted (full scans pass False).
        """
        started = time.monotonic()
        report = ScanReport()
        for root in roots:
            root_path = Path(root)
            if not root_path.exists():
                if prune and prune_missing:
                    report.removed += self.remove_under(root_path)
                else:
                    log.warning("library: %s is missing — keeping its tracks", root_path)
                continue
            with self._lock:
                known = {r["path"]: _row_dict(r) for r in self._conn.execute(
                    "SELECT path, size, mtime, added_at FROM tracks WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                    (str(root_path), _like_prefix(root_path)))}
            seen: set[str] = set()
            for path in iter_audio_files(root_path):
                key = str(path)
                seen.add(key)
                report.scanned += 1
                try:
                    st = path.stat()
                except OSError:
                    report.errors += 1
                    continue
                old = known.get(key)
                if old and old["size"] == st.st_size and abs(old["mtime"] - st.st_mtime) < 1e-6:
                    continue
                try:
                    self._upsert(path, st, old)
                except Exception as exc:  # noqa: BLE001 - one bad file mustn't stop a scan
                    log.warning("library: failed to index %s: %s", path, exc)
                    report.errors += 1
                    continue
                if old:
                    report.updated += 1
                else:
                    report.added += 1
            if prune:
                gone = [p for p in known if p not in seen]
                with self._lock:
                    for p in gone:
                        self._conn.execute("DELETE FROM tracks WHERE path = ?", (p,))
                report.removed += len(gone)
            with self._lock:
                self._conn.commit()
        report.seconds = time.monotonic() - started
        return report

    def remove_under(self, path: str | Path) -> int:
        """Drop a file's row, or every row under a folder. Returns rows removed."""
        p = Path(path)
        with self._lock:
            cur = self._conn.execute("DELETE FROM tracks WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                                     (str(p), _like_prefix(p)))
            self._conn.commit()
            return cur.rowcount or 0

    def prune_outside(self, roots: Iterable[str | Path]) -> int:
        """Drop rows that no longer sit under any configured library folder."""
        roots = [resolve(r) for r in roots]
        with self._lock:
            paths = [r["path"] for r in self._conn.execute("SELECT path FROM tracks")]
            gone = [p for p in paths if not within(p, roots)]
            for p in gone:
                self._conn.execute("DELETE FROM tracks WHERE path = ?", (p,))
            self._conn.commit()
        return len(gone)

    # -- reads ------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS t, COUNT(DISTINCT album_id) AS al, "
                "COUNT(DISTINCT album_artist_id) AS ar FROM tracks").fetchone()
        return {"tracks": row["t"], "albums": row["al"], "artists": row["ar"]}

    def track(self, track_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return _row_dict(row) if row else None

    _TRACK_ORDER = "COALESCE(disc_no, 1), COALESCE(track_no, 9999), title COLLATE NOCASE"

    def album_tracks(self, album_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tracks WHERE album_id = ? ORDER BY {self._TRACK_ORDER}",
                (album_id,)).fetchall()
        return [_row_dict(r) for r in rows]

    _ALBUM_SELECT = (
        "SELECT album_id, MIN(album) AS album, MIN(album_artist) AS album_artist, "
        "MIN(album_artist_id) AS album_artist_id, MAX(year) AS year, MIN(date) AS date, "
        "COUNT(*) AS track_count, MAX(mb_releasegroup) AS mb_releasegroup, "
        "MIN(path) AS path, MAX(added_at) AS added_at FROM tracks")

    def album(self, album_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                f"{self._ALBUM_SELECT} WHERE album_id = ? GROUP BY album_id",
                (album_id,)).fetchone()
        return _row_dict(row) if row else None

    def albums(self, *, artist_id: str | None = None, order: str = "year",
               limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        where, args = "", []
        if artist_id:
            where, args = "WHERE album_artist_id = ?", [artist_id]
        order_sql = {
            "year": "year IS NULL, year, album COLLATE NOCASE",
            "recent": "added_at DESC",
            "title": "album COLLATE NOCASE",
            "artist": "album_artist COLLATE NOCASE, year IS NULL, year",
        }.get(order, "year IS NULL, year")
        with self._lock:
            rows = self._conn.execute(
                f"{self._ALBUM_SELECT} {where} GROUP BY album_id ORDER BY {order_sql} "
                "LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return [_row_dict(r) for r in rows]

    def artists(self, *, limit: int = 2000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT album_artist_id AS id, MIN(album_artist) AS name, "
                "COUNT(DISTINCT album_id) AS album_count, MAX(mb_artist) AS mb_artist "
                "FROM tracks GROUP BY album_artist_id ORDER BY name COLLATE NOCASE LIMIT ?",
                (limit,)).fetchall()
        return [_row_dict(r) for r in rows]

    def artist(self, artist_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ? AS id, COALESCE("
                " (SELECT MIN(album_artist) FROM tracks WHERE album_artist_id = ?),"
                " (SELECT MIN(artist) FROM tracks WHERE artist_id = ?)) AS name",
                (artist_id, artist_id, artist_id)).fetchone()
        return _row_dict(row) if row and row["name"] else None

    def artist_tracks(self, artist_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """An artist's tracks — their own albums first, then guest appearances."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE album_artist_id = ? OR artist_id = ? "
                f"ORDER BY album_artist_id != ?, year IS NULL, year, album_id, {self._TRACK_ORDER} "
                "LIMIT ?", (artist_id, artist_id, artist_id, limit)).fetchall()
        return [_row_dict(r) for r in rows]

    def search(self, query: str, *, kinds: Iterable[str] = ("tracks",),
               limit: int = 25) -> dict[str, list[dict[str, Any]]]:
        """Every query token must appear (title/artist/album); ranked by fuzz."""
        from rapidfuzz import fuzz

        kinds = set(kinds)
        tokens = norm(query).split()
        out: dict[str, list[dict[str, Any]]] = {"tracks": [], "albums": [], "artists": []}
        if not tokens:
            return out
        where = " AND ".join("haystack LIKE ?" for _ in tokens)
        args = [f"%{t}%" for t in tokens]
        q = norm(query)
        if "tracks" in kinds:
            with self._lock:
                rows = self._conn.execute(f"SELECT * FROM tracks WHERE {where} LIMIT 400",
                                          args).fetchall()
            ranked = sorted((_row_dict(r) for r in rows), key=lambda r: -max(
                fuzz.token_set_ratio(q, norm(r["title"])),
                fuzz.token_set_ratio(q, norm(f"{r['artist']} {r['title']}"))))
            out["tracks"] = ranked[:limit]
        if "albums" in kinds:
            with self._lock:
                rows = self._conn.execute(
                    f"{self._ALBUM_SELECT} WHERE album_id IN "
                    f"(SELECT album_id FROM tracks WHERE {where}) GROUP BY album_id LIMIT 200",
                    args).fetchall()
            ranked = sorted((_row_dict(r) for r in rows), key=lambda r: -max(
                fuzz.token_set_ratio(q, norm(r["album"])),
                fuzz.token_set_ratio(q, norm(f"{r['album_artist']} {r['album']}"))))
            # An album only counts when the query is about the album/artist, not
            # merely one of its track titles.
            out["albums"] = [r for r in ranked if fuzz.token_set_ratio(
                q, norm(f"{r['album_artist']} {r['album']}")) >= 60][:limit]
        if "artists" in kinds:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT album_artist_id AS id, MIN(album_artist) AS name FROM tracks "
                    f"WHERE {where} GROUP BY album_artist_id LIMIT 200", args).fetchall()
            ranked = [r for r in (_row_dict(x) for x in rows)
                      if fuzz.token_set_ratio(q, norm(r["name"])) >= 60]
            ranked.sort(key=lambda r: -fuzz.token_sort_ratio(q, norm(r["name"])))
            out["artists"] = ranked[:limit]
        return out

    def find_album(self, title: str, artist: str = "", *,
                   mbid: str | None = None) -> dict[str, Any] | None:
        """The library's copy of an album: exact by MusicBrainz release-group id
        when known, else a confident title (+artist) match."""
        from rapidfuzz import fuzz

        if mbid:
            with self._lock:
                row = self._conn.execute(
                    f"{self._ALBUM_SELECT} WHERE mb_releasegroup = ? OR mb_release = ? "
                    "GROUP BY album_id LIMIT 1", (mbid, mbid)).fetchone()
            if row:
                return _row_dict(row)
        nt, na = norm(title), norm(artist)
        if not nt:
            return None
        # Prefilter on the title's longest word, then score properly.
        word = max(nt.split(), key=len)
        with self._lock:
            rows = self._conn.execute(
                f"{self._ALBUM_SELECT} WHERE haystack LIKE ? GROUP BY album_id LIMIT 300",
                (f"%{word}%",)).fetchall()
        best, best_score = None, 0.0
        for r in rows:
            score = fuzz.token_sort_ratio(nt, norm(r["album"]))
            if na:
                score = 0.65 * score + 0.35 * fuzz.token_sort_ratio(na, norm(r["album_artist"]))
            if score > best_score and score >= 85:
                best, best_score = _row_dict(r), score
        return best

    def album_art(self, album_id: str) -> tuple[bytes, str] | None:
        """Cover bytes for an album: a folder image, else the embedded picture."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT path FROM tracks WHERE album_id = ? ORDER BY {self._TRACK_ORDER} LIMIT 3",
                (album_id,)).fetchall()
        if not rows:
            return None
        first = Path(rows[0]["path"])
        for directory in (first.parent, first.parent.parent):
            cover = folder_cover(directory)
            if cover is not None:
                try:
                    return cover.read_bytes(), _IMAGE_TYPES[cover.suffix.lower()]
                except OSError:
                    break
            # Only look one level up when the files sit in a "CD1"-style folder.
            if not re.fullmatch(r"(?i)(cd|disc|disk)\s*\d+", first.parent.name):
                break
        for r in rows:
            art = embedded_cover(Path(r["path"]))
            if art:
                return art
        return None


def _like_prefix(path: Path) -> str:
    """A LIKE pattern for everything strictly under ``path`` (wildcards escaped)."""
    base = str(path).rstrip("/") + "/"
    return base.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


__all__ = [
    "AUDIO_TYPES", "ArtSigner", "FileTags", "LibraryIndex", "ScanReport", "album_id_for",
    "artist_id_for", "has_mutagen", "iter_audio_files", "map_path", "norm", "read_tags",
    "resolve", "track_id_for", "within",
]
