"""MusicBrainz enrichment: ISRC lookup and canonical recording metadata.

No API key is required, but the service demands a descriptive User-Agent
(handled by the shared ``_get_json`` helper) and a hard 1 request/second rate
limit, enforced here via a module-scoped ``RateLimiter`` so every caller
shares one clock regardless of how many functions they call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from rapidfuzz import fuzz

from ..tasks import RateLimiter
from . import _cache_key, _cached_call, _get_json

if TYPE_CHECKING:
    from ..db import Database

log = logging.getLogger(__name__)

API_URL = "https://musicbrainz.org/ws/2"

# MusicBrainz's own etiquette guideline is "no more than one request per
# second, sustained" — the small margin avoids edge-of-window 503s.
_rate_limiter = RateLimiter(1.05)

# Accept an artist-credit as "the same artist" above this rapidfuzz score
# (0..100). Loose enough to tolerate "The Beatles" vs "Beatles, The" or
# minor transliteration differences, tight enough to reject a same-titled
# recording by an unrelated act.
_ARTIST_MATCH_THRESHOLD = 60.0


def _search_recordings(artist: str, title: str, db: Database | None) -> list[dict[str, Any]]:
    """Lucene-search MusicBrainz recordings for ``artist``/``title``."""
    query = f'artist:"{artist}" AND recording:"{title}"'
    key = _cache_key("musicbrainz", "search", f"{artist}|{title}")

    def fetch() -> Any:
        return _get_json(
            f"{API_URL}/recording/",
            {"query": query, "fmt": "json", "limit": 10},
            rate_limiter=_rate_limiter,
        )

    payload = _cached_call(db, key, fetch)
    return payload.get("recordings", []) if isinstance(payload, dict) else []


def _artist_credit_name(recording: dict[str, Any]) -> str:
    credits = recording.get("artist-credit") or []
    return " ".join(c.get("name", "") for c in credits if isinstance(c, dict)).strip()


def _artist_matches(wanted: str, recording: dict[str, Any]) -> bool:
    credited = _artist_credit_name(recording)
    if not credited:
        return False
    return fuzz.token_sort_ratio(wanted.lower(), credited.lower()) >= _ARTIST_MATCH_THRESHOLD


def lookup_isrc(artist: str, title: str, *, db: Database | None = None) -> str | None:
    """The ISRC of the best-scoring recording match, or ``None`` if none qualify.

    "Best-scoring" is MusicBrainz's own relevance ``score``; artist identity
    is verified separately before a candidate is accepted, since the search
    can return a same-titled recording by a completely different act.
    """
    recordings = _search_recordings(artist, title, db)
    candidates = [r for r in recordings if r.get("isrcs")]
    candidates.sort(key=lambda r: r.get("score", 0), reverse=True)
    for recording in candidates:
        if _artist_matches(artist, recording):
            isrcs = recording.get("isrcs") or []
            if isrcs:
                return isrcs[0]
    return None


def canonical_metadata(artist: str, title: str, *, db: Database | None = None) -> dict[str, Any] | None:
    """Canonical ``{title, artist, release, year, mbid, isrcs}`` for the best match."""
    recordings = sorted(_search_recordings(artist, title, db), key=lambda r: r.get("score", 0), reverse=True)
    for recording in recordings:
        if not _artist_matches(artist, recording):
            continue
        releases = recording.get("releases") or []
        release = releases[0] if releases else {}
        year = None
        date = release.get("date", "")
        if date:
            try:
                year = int(date.split("-")[0])
            except ValueError:
                year = None
        return {
            "title": recording.get("title", title),
            "artist": _artist_credit_name(recording) or artist,
            "release": release.get("title", ""),
            "year": year,
            "mbid": recording.get("id", ""),
            "isrcs": recording.get("isrcs") or [],
        }
    return None


def recordings_by_isrc(isrc: str, *, db: Database | None = None) -> list[dict[str, Any]]:
    """Raw MusicBrainz recording objects registered under ``isrc``."""
    key = _cache_key("musicbrainz", "isrc", isrc)

    def fetch() -> Any:
        return _get_json(
            f"{API_URL}/isrc/{isrc}",
            {"fmt": "json", "inc": "artist-credits+releases"},
            rate_limiter=_rate_limiter,
        )

    payload = _cached_call(db, key, fetch)
    return payload.get("recordings", []) if isinstance(payload, dict) else []
