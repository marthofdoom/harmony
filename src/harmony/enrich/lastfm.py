"""Last.fm enrichment: similar artists/tracks, tags, and per-tag top tracks.

Every function raises ``MissingCredentialError`` when no API key is
configured and, when given a ``db``, caches the raw JSON response for a week
before parsing it into dataclasses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import config
from ..config import CredentialStore
from ..errors import MissingCredentialError
from . import _cache_key, _cached_call, _get_json

if TYPE_CHECKING:
    from ..db import Database

log = logging.getLogger(__name__)

API_URL = "https://ws.audioscrobbler.com/2.0/"


@dataclass(slots=True)
class SimilarTrack:
    """A track Last.fm considers similar to (or popular for) a seed."""

    name: str
    artist: str
    match: float = 0.0
    url: str = ""
    mbid: str = ""


@dataclass(slots=True)
class SimilarArtist:
    name: str
    match: float = 0.0
    url: str = ""
    mbid: str = ""


def _api_key() -> str:
    key = CredentialStore().get(config.LASTFM_API_KEY)
    if not key:
        raise MissingCredentialError(
            "Last.fm API key",
            hint="Add one in Preferences → Integrations. Free keys: https://www.last.fm/api/account/create",
        )
    return key


def _call(method: str, params: dict[str, Any], db: Database | None, cache_suffix: str) -> dict[str, Any]:
    """Issue (or replay from cache) a single Last.fm ``method`` call."""
    query = {"method": method, "format": "json", "api_key": _api_key(), **params}
    key = _cache_key("lastfm", method, cache_suffix)

    def fetch() -> Any:
        return _get_json(API_URL, query)

    payload = _cached_call(db, key, fetch)
    if isinstance(payload, dict) and "error" in payload:
        raise_msg = payload.get("message", "unknown Last.fm error")
        log.warning("Last.fm %s returned an error: %s", method, raise_msg)
        return {}
    return payload if isinstance(payload, dict) else {}


def _coerce_list(value: Any) -> list[dict[str, Any]]:
    """Last.fm returns a bare object instead of a list when there's one result."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_similar_track(raw: dict[str, Any]) -> SimilarTrack:
    artist = raw.get("artist")
    artist_name = artist.get("name", "") if isinstance(artist, dict) else str(artist or "")
    return SimilarTrack(
        name=raw.get("name", ""),
        artist=artist_name,
        match=_as_float(raw.get("match", 0.0)),
        url=raw.get("url", ""),
        mbid=raw.get("mbid", ""),
    )


def _parse_similar_artist(raw: dict[str, Any]) -> SimilarArtist:
    return SimilarArtist(
        name=raw.get("name", ""),
        match=_as_float(raw.get("match", 0.0)),
        url=raw.get("url", ""),
        mbid=raw.get("mbid", ""),
    )


def similar_tracks(
    artist: str, title: str, *, limit: int = 30, db: Database | None = None
) -> list[SimilarTrack]:
    """Tracks Last.fm's collaborative filter considers similar to ``artist``/``title``."""
    payload = _call(
        "track.getsimilar",
        {"artist": artist, "track": title, "autocorrect": 1, "limit": limit},
        db,
        f"{artist}|{title}|{limit}",
    )
    raw_tracks = _coerce_list(payload.get("similartracks", {}).get("track") if payload else None)
    return [_parse_similar_track(t) for t in raw_tracks]


def similar_artists(artist: str, *, limit: int = 30, db: Database | None = None) -> list[SimilarArtist]:
    """Artists Last.fm's collaborative filter considers similar to ``artist``."""
    payload = _call(
        "artist.getsimilar", {"artist": artist, "autocorrect": 1, "limit": limit}, db, f"{artist}|{limit}"
    )
    raw_artists = _coerce_list(payload.get("similarartists", {}).get("artist") if payload else None)
    return [_parse_similar_artist(a) for a in raw_artists]


def top_tracks_for_artist(
    artist: str, *, limit: int = 30, db: Database | None = None
) -> list[SimilarTrack]:
    """An artist's most-played tracks; ``match`` is playcount normalised 0..1."""
    payload = _call(
        "artist.gettoptracks", {"artist": artist, "autocorrect": 1, "limit": limit}, db, f"{artist}|{limit}"
    )
    raw_tracks = _coerce_list(payload.get("toptracks", {}).get("track") if payload else None)
    tracks = [_parse_similar_track(t) for t in raw_tracks]
    counts = []
    for raw in raw_tracks:
        try:
            counts.append(int(raw.get("playcount", 0)))
        except (TypeError, ValueError):
            counts.append(0)
    peak = max(counts, default=0)
    if peak:
        for track, count in zip(tracks, counts, strict=True):
            track.match = count / peak
    return tracks


def tags_for_track(artist: str, title: str, *, db: Database | None = None) -> list[str]:
    """Community tags for a specific recording, most-applied first."""
    payload = _call(
        "track.gettoptags", {"artist": artist, "track": title, "autocorrect": 1}, db, f"{artist}|{title}"
    )
    raw_tags = _coerce_list(payload.get("toptags", {}).get("tag") if payload else None)
    return [t.get("name", "") for t in raw_tags if t.get("name")]


def tags_for_artist(artist: str, *, db: Database | None = None) -> list[str]:
    """Community tags for an artist, most-applied first."""
    payload = _call("artist.gettoptags", {"artist": artist, "autocorrect": 1}, db, artist)
    raw_tags = _coerce_list(payload.get("toptags", {}).get("tag") if payload else None)
    return [t.get("name", "") for t in raw_tags if t.get("name")]


def top_tracks_for_tag(tag: str, *, limit: int = 50, db: Database | None = None) -> list[SimilarTrack]:
    """The most popular tracks carrying a given community tag."""
    payload = _call("tag.gettoptracks", {"tag": tag, "limit": limit}, db, f"{tag}|{limit}")
    # tag.getTopTracks nests its list under "tracks", unlike the other
    # endpoints above which nest under the method-shaped key.
    raw_tracks = _coerce_list(payload.get("tracks", {}).get("track") if payload else None)
    return [_parse_similar_track(t) for t in raw_tracks]
