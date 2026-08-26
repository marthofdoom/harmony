"""M3U / CSV / JSON import & export.

Export writes real ``Track``/``Playlist`` objects out. Import is deliberately
"loose": M3U and CSV cannot carry a stable cross-service id, so the three
importers hand back plain descriptor dicts (``{title, artists, album,
duration_s, isrc}``) rather than ``Track`` objects, and ``resolve_imported``
re-resolves those descriptors against a live provider through
``matching.match_track`` — the same path a hand-typed playlist would take.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import matching
from .matching import MatchResult
from .models import Playlist, Service, Track

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .providers.base import MusicProvider

log = logging.getLogger(__name__)

TrackDescriptor = dict[str, Any]


# --------------------------------------------------------------------------
# Shared (de)serialization — also used by sync.py for playlist snapshots.
# --------------------------------------------------------------------------


def track_to_dict(track: Track) -> dict[str, Any]:
    """JSON-safe, round-trippable ``Track`` representation.

    ``raw`` (the provider's opaque native payload) is intentionally dropped:
    it isn't guaranteed JSON-serialisable and has no meaning once resolved
    back on a possibly different service.
    """
    return {
        "id": track.id,
        "title": track.title,
        "service": track.service.value,
        "artists": list(track.artists),
        "album": track.album,
        "duration_s": track.duration_s,
        "isrc": track.isrc,
        "year": track.year,
        "track_number": track.track_number,
        "artwork_url": track.artwork_url,
        "explicit": track.explicit,
        "play_count": track.play_count,
    }


def track_from_dict(d: dict[str, Any]) -> Track:
    return Track(
        id=d["id"],
        title=d["title"],
        service=Service(d["service"]),
        artists=list(d.get("artists") or []),
        album=d.get("album"),
        duration_s=d.get("duration_s"),
        isrc=d.get("isrc"),
        year=d.get("year"),
        track_number=d.get("track_number"),
        artwork_url=d.get("artwork_url"),
        explicit=bool(d.get("explicit", False)),
        play_count=d.get("play_count"),
    )


def playlist_to_dict(playlist: Playlist) -> dict[str, Any]:
    return {
        "id": playlist.id,
        "title": playlist.title,
        "service": playlist.service.value,
        "description": playlist.description,
        "track_count": playlist.track_count,
        "owner": playlist.owner,
        "public": playlist.public,
        "artwork_url": playlist.artwork_url,
    }


def playlist_from_dict(d: dict[str, Any]) -> Playlist:
    return Playlist(
        id=d["id"],
        title=d["title"],
        service=Service(d["service"]),
        description=d.get("description", ""),
        track_count=d.get("track_count"),
        owner=d.get("owner"),
        public=bool(d.get("public", False)),
        artwork_url=d.get("artwork_url"),
    )


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def export_m3u(tracks: Sequence[Track], path: str | Path) -> None:
    """Write an Extended M3U playlist.

    The "location" line can't be a real filesystem path (these are streaming
    catalog entries, not local files), so we emit a ``service:id`` URI-ish
    token. It plays nothing on its own but round-trips within Harmony and is
    still syntactically a valid M3U entry.
    """
    lines = ["#EXTM3U"]
    for track in tracks:
        duration = track.duration_s if track.duration_s is not None else -1
        lines.append(f"#EXTINF:{duration},{track.artist_name} - {track.title}")
        lines.append(f"{track.service.value}:{track.id}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def export_csv(tracks: Sequence[Track], path: str | Path) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["title", "artists", "album", "duration_s", "isrc", "service", "id"])
        for track in tracks:
            writer.writerow(
                [
                    track.title,
                    "; ".join(track.artists),
                    track.album or "",
                    track.duration_s if track.duration_s is not None else "",
                    track.isrc or "",
                    track.service.value,
                    track.id,
                ]
            )


def export_json(playlist: Playlist, tracks: Sequence[Track], path: str | Path) -> None:
    """Full round-trippable snapshot: playlist metadata + every track."""
    payload = {"playlist": playlist_to_dict(playlist), "tracks": [track_to_dict(t) for t in tracks]}
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


# An ISRC is 2 letters + 3 alphanumerics + 7 digits (e.g. USRC17607839). Used
# to recognise an optional trailing universal id on a text-list line.
_ISRC_RE = re.compile(r"^[A-Za-z]{2}[A-Za-z0-9]{3}\d{7}$")


def export_txt(tracks: Sequence[Track], path: str | Path, *, with_isrc: bool = True) -> None:
    """Write a plain, human-readable song list: one ``Artist - Title`` per line.

    The simplest possible interchange format — readable, greppable, pasteable
    anywhere. When ``with_isrc`` and a track carries an ISRC (the universal
    recording id), it's appended after a tab so the list still round-trips
    exactly; a track without one is just its ``Artist - Title``. ``import_txt``
    reads both shapes back.
    """
    lines = []
    for track in tracks:
        name = f"{track.artist_name} - {track.title}" if track.artist_name else track.title
        if with_isrc and track.isrc:
            lines.append(f"{name}\t{track.isrc}")
        else:
            lines.append(name)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Import — loose descriptors, malformed rows are skipped with a warning
# --------------------------------------------------------------------------


def import_m3u(path: str | Path) -> tuple[list[TrackDescriptor], list[str]]:
    descriptors: list[TrackDescriptor] = []
    warnings: list[str] = []
    pending: TrackDescriptor | None = None

    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line == "#EXTM3U":
            continue
        if line.startswith("#EXTINF:"):
            pending = _parse_extinf(line, lineno, warnings)
        elif line.startswith("#"):
            continue  # unsupported extension directive, ignored
        else:
            if pending is not None:
                descriptors.append(pending)
                pending = None
            else:
                warnings.append(f"line {lineno}: location line with no preceding #EXTINF, skipped")
    return descriptors, warnings


def _parse_extinf(line: str, lineno: int, warnings: list[str]) -> TrackDescriptor | None:
    body = line[len("#EXTINF:") :]
    duration_str, sep, rest = body.partition(",")
    if not sep:
        warnings.append(f"line {lineno}: malformed #EXTINF (no comma), skipped")
        return None
    try:
        duration_s: int | None = int(float(duration_str))
    except ValueError:
        duration_s = None
    artist, sep2, title = rest.partition(" - ")
    if not sep2:
        warnings.append(f"line {lineno}: could not split \"artist - title\" from {rest!r}, skipped")
        return None
    if not title.strip():
        warnings.append(f"line {lineno}: empty title, skipped")
        return None
    return {
        "title": title.strip(),
        "artists": [artist.strip()] if artist.strip() else [],
        "album": None,
        "duration_s": duration_s if duration_s and duration_s > 0 else None,
        "isrc": None,
    }


def import_csv(path: str | Path) -> tuple[list[TrackDescriptor], list[str]]:
    descriptors: list[TrackDescriptor] = []
    warnings: list[str] = []
    with Path(path).open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=2):  # header occupies line 1
            try:
                descriptors.append(_row_to_descriptor(row))
            except (ValueError, KeyError) as exc:
                warnings.append(f"row {i}: {exc}, skipped")
    return descriptors, warnings


def _row_to_descriptor(row: dict[str, str | None]) -> TrackDescriptor:
    title = (row.get("title") or "").strip()
    if not title:
        raise ValueError("missing title")
    artists_field = (row.get("artists") or "").strip()
    artists = [a.strip() for a in artists_field.split(";") if a.strip()]
    duration_raw = (row.get("duration_s") or "").strip()
    duration_s = int(duration_raw) if duration_raw else None
    return {
        "title": title,
        "artists": artists,
        "album": (row.get("album") or "").strip() or None,
        "duration_s": duration_s,
        "isrc": (row.get("isrc") or "").strip() or None,
    }


def import_txt(path: str | Path) -> tuple[list[TrackDescriptor], list[str]]:
    """Parse a plain song list (``Artist - Title`` per line, optional tab + ISRC).

    Blank lines and ``#`` comment lines are ignored, so a hand-annotated list
    still imports. A line with no ``" - "`` is treated as a bare title (no
    artist) rather than dropped — the matcher can still resolve it.
    """
    descriptors: list[TrackDescriptor] = []
    warnings: list[str] = []
    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        isrc: str | None = None
        name = line
        if "\t" in line:
            name_part, _, tail = line.rpartition("\t")
            tail = tail.strip()
            if _ISRC_RE.match(tail):
                isrc = tail.upper()
                name = name_part.strip()
        artist, sep, title = name.partition(" - ")
        if sep:
            artists = [artist.strip()] if artist.strip() else []
            title = title.strip()
        else:
            artists = []
            title = name.strip()  # no separator -> the whole line is the title
        if not title:
            warnings.append(f"line {lineno}: empty title, skipped")
            continue
        descriptors.append(
            {"title": title, "artists": artists, "album": None, "duration_s": None, "isrc": isrc}
        )
    return descriptors, warnings


def import_json(path: str | Path) -> tuple[list[TrackDescriptor], list[str]]:
    warnings: list[str] = []
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"could not read {path}: {exc}"]

    items = raw.get("tracks") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return [], ["expected a top-level list or a {'tracks': [...]} object"]

    descriptors: list[TrackDescriptor] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            warnings.append(f"item {i}: missing or invalid 'title', skipped")
            continue
        descriptors.append(
            {
                "title": item["title"],
                "artists": list(item.get("artists") or []),
                "album": item.get("album"),
                "duration_s": item.get("duration_s"),
                "isrc": item.get("isrc"),
            }
        )
    return descriptors, warnings


# --------------------------------------------------------------------------
# Resolution back to real catalog tracks
# --------------------------------------------------------------------------


def resolve_imported(
    descriptors: Sequence[TrackDescriptor], provider: MusicProvider
) -> list[MatchResult]:
    """Resolve loose import descriptors into real tracks on ``provider``.

    Returns full ``MatchResult`` objects (not just the winning ``Track``) so
    a caller — typically the import UI — can show low-confidence guesses for
    manual confirmation instead of silently accepting a bad match.
    """
    results = []
    for d in descriptors:
        source = Track(
            id="",
            title=str(d.get("title") or ""),
            service=provider.service,
            artists=list(d.get("artists") or []),
            album=d.get("album"),
            duration_s=d.get("duration_s"),
            isrc=d.get("isrc"),
        )
        results.append(matching.match_track(source, provider))
    return results
