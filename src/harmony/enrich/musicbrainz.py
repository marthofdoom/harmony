"""MusicBrainz enrichment: ISRC lookup and canonical recording metadata.

No API key is required, but the service demands a descriptive User-Agent
(handled by the shared ``_get_json`` helper) and a hard 1 request/second rate
limit, enforced here via a module-scoped ``RateLimiter`` so every caller
shares one clock regardless of how many functions they call.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from rapidfuzz import fuzz

from ..errors import ProviderError
from ..tasks import RateLimiter
from . import _cache_key, _cached_call, _get_json

if TYPE_CHECKING:
    from ..db import Database

log = logging.getLogger(__name__)

API_URL = "https://musicbrainz.org/ws/2"

# MusicBrainz's own etiquette guideline is "no more than one request per
# second, sustained" — the small margin avoids edge-of-window 503s.
_rate_limiter = RateLimiter(1.05)


def _mb_get(url: str, params: dict[str, Any]) -> Any:
    """GET a MusicBrainz URL, backing off and retrying its routine 503s.

    MusicBrainz returns ``503 "currently busy"`` under load and expects clients
    to wait and retry rather than treat it as a hard failure — so the shared
    ``_get_json`` (which raises on any non-429 error) is wrapped here with a
    short exponential backoff. Everything else (429, 4xx, transport) still
    propagates as a ``ProviderError``/``RateLimitedError`` on the first hit.
    """
    delay = 1.0
    for attempt in range(4):
        try:
            return _get_json(url, params, rate_limiter=_rate_limiter)
        except ProviderError as exc:
            if "HTTP 503" in str(exc) and attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    return None  # unreachable; keeps type-checkers happy

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
        return _mb_get(
            f"{API_URL}/recording/",
            {"query": query, "fmt": "json", "limit": 10},
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
        return _mb_get(
            f"{API_URL}/isrc/{isrc}",
            {"fmt": "json", "inc": "artist-credits+releases"},
        )

    payload = _cached_call(db, key, fetch)
    return payload.get("recordings", []) if isinstance(payload, dict) else []


# --------------------------------------------------------------------------
# Entity graph: artists, memberships, discographies, per-recording performers.
#
# These power the artist/album/track detail pages and the member-chronology
# chart. Everything is cached for a week (:data:`CACHE_TTL_S`) and shares the
# 1 req/s rate limiter, so a warm artist page costs zero live requests.
# --------------------------------------------------------------------------

_LUCENE_SPECIAL = r'+-&&||!(){}[]^"~*?:\/'


def _lucene_escape(text: str) -> str:
    """Escape Lucene query metacharacters so a name with ``:`` or ``-`` is literal."""
    out = []
    for ch in text:
        if ch in _LUCENE_SPECIAL:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _year_of(date: str | None) -> int | None:
    if not date:
        return None
    try:
        return int(str(date).split("-")[0])
    except (ValueError, TypeError):
        return None


def search_artist(name: str, *, db: Database | None = None, limit: int = 8) -> list[dict[str, Any]]:
    """Best artist matches for ``name`` — ``[{mbid, name, type, disambiguation, score, country}]``.

    ``type`` is MusicBrainz's ``"Person"`` / ``"Group"`` (or ``None``); the caller
    uses it to decide between a group discography and a person's performed-on list.
    """
    query = f'artist:"{_lucene_escape(name)}"'
    key = _cache_key("musicbrainz", "artist-search", name.lower())

    def fetch() -> Any:
        return _mb_get(
            f"{API_URL}/artist/",
            {"query": query, "fmt": "json", "limit": limit},
        )

    payload = _cached_call(db, key, fetch)
    artists = payload.get("artists", []) if isinstance(payload, dict) else []
    out: list[dict[str, Any]] = []
    for a in artists:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        out.append({
            "mbid": a.get("id", ""),
            "name": a.get("name", ""),
            "type": a.get("type"),
            "disambiguation": a.get("disambiguation", ""),
            "score": int(a.get("score", 0) or 0),
            "country": a.get("country", ""),
        })
    return out


def resolve_artist(name: str, *, db: Database | None = None, prefer_type: str | None = None) -> dict[str, Any] | None:
    """The single best artist match for ``name`` (fuzzy-verified), or ``None``.

    ``prefer_type`` ("Person"/"Group") nudges ties toward the wanted kind but
    never overrides a clearly-better-scoring match of the other kind.
    """
    candidates = search_artist(name, db=db)
    best: dict[str, Any] | None = None
    best_score = 0.0
    for c in candidates:
        name_sim = fuzz.token_sort_ratio(name.lower(), c["name"].lower())
        if name_sim < _ARTIST_MATCH_THRESHOLD:
            continue
        score = name_sim + c["score"] / 10.0
        if prefer_type and c.get("type") == prefer_type:
            score += 8.0
        if score > best_score:
            best_score, best = score, c
    return best


def artist_lookup(mbid: str, *, db: Database | None = None) -> dict[str, Any]:
    """Full artist lookup with membership + URL relations and tags."""
    key = _cache_key("musicbrainz", "artist", mbid)

    def fetch() -> Any:
        return _mb_get(
            f"{API_URL}/artist/{mbid}",
            {"fmt": "json", "inc": "artist-rels+url-rels+tags+aliases"},
        )

    payload = _cached_call(db, key, fetch)
    return payload if isinstance(payload, dict) else {}


def _membership_relations(payload: dict[str, Any], *, direction: str) -> list[dict[str, Any]]:
    """Parse ``member of band`` relations, grouping repeat stints into spans.

    ``direction="backward"`` (a group lookup) yields the *people*; ``"forward"``
    (a person lookup) yields the *bands*. Each entry: ``{name, mbid, instruments,
    spans:[[start,end|None]...], is_current}``.
    """
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for rel in payload.get("relations", []) or []:
        if not isinstance(rel, dict) or rel.get("type") != "member of band":
            continue
        if rel.get("direction") != direction:
            continue
        other = rel.get("artist") or {}
        mbid = other.get("id") or other.get("name") or ""
        if not mbid:
            continue
        entry = grouped.get(mbid)
        if entry is None:
            entry = {"name": other.get("name", ""), "mbid": other.get("id"),
                     "instruments": [], "spans": [], "is_current": False}
            grouped[mbid] = entry
            order.append(mbid)
        for attr in rel.get("attributes", []) or []:
            # "original" is a membership attribute (original member), not an
            # instrument — keep it out of the instrument list shown on the chart.
            if attr and attr != "original" and attr not in entry["instruments"]:
                entry["instruments"].append(attr)
        begin = _year_of(rel.get("begin"))
        end = _year_of(rel.get("end"))
        ended = bool(rel.get("ended"))
        if not ended and end is None:
            entry["is_current"] = True
        span = [begin, end if ended else None]
        # MB emits one "member of band" relation per sub-role/instrument, so the
        # same tenure arrives several times — keep only distinct [start, end] pairs.
        if span not in entry["spans"]:
            entry["spans"].append(span)
    # Stable order: earliest span first, then join order.
    result = [grouped[m] for m in order]
    for entry in result:
        entry["spans"].sort(key=lambda s: (s[0] is None, s[0] or 0))
    result.sort(key=lambda e: min((s[0] for s in e["spans"] if s[0] is not None), default=9999))
    return result


def members_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """People who were in this group (from a group's ``artist_lookup``)."""
    return _membership_relations(payload, direction="backward")


def bands_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Bands this person was a member of (from a person's ``artist_lookup``)."""
    return _membership_relations(payload, direction="forward")


def artist_urls(payload: dict[str, Any]) -> dict[str, str]:
    """Map of ``{rel_type: url}`` from an artist's URL relations (wikidata, wikipedia, ...)."""
    urls: dict[str, str] = {}
    for rel in payload.get("relations", []) or []:
        if not isinstance(rel, dict):
            continue
        url = (rel.get("url") or {}).get("resource")
        rtype = rel.get("type")
        if url and rtype and rtype not in urls:
            urls[rtype] = url
    return urls


# Secondary-types we treat as "not a studio album" when splitting a discography.
_NON_ALBUM_SECONDARY = {"Compilation", "Live", "Soundtrack", "Remix", "DJ-mix", "Mixtape/Street", "Demo"}


def release_groups(mbid: str, *, db: Database | None = None, cap: int = 200) -> list[dict[str, Any]]:
    """All release-groups credited to ``mbid`` — ``[{title, year, date, primary, secondary, mbid}]``.

    Paginated (MB caps a browse at 100) up to ``cap``, sorted by release date ASC.
    """
    key = _cache_key("musicbrainz", "rgs", mbid)

    def fetch() -> Any:
        collected: list[dict[str, Any]] = []
        offset = 0
        while offset < cap:
            page = _mb_get(
                f"{API_URL}/release-group",
                {"artist": mbid, "fmt": "json", "limit": 100, "offset": offset,
                 "type": "album|ep|single"},
            )
            rgs = page.get("release-groups", []) if isinstance(page, dict) else []
            collected.extend(rgs)
            total = page.get("release-group-count", 0) if isinstance(page, dict) else 0
            offset += 100
            if offset >= total or not rgs:
                break
        return collected

    raw = _cached_call(db, key, fetch)
    out: list[dict[str, Any]] = []
    for rg in raw if isinstance(raw, list) else []:
        if not isinstance(rg, dict):
            continue
        date = rg.get("first-release-date") or ""
        out.append({
            "mbid": rg.get("id", ""),
            "title": rg.get("title", ""),
            "date": date,
            "year": _year_of(date),
            "primary": rg.get("primary-type") or "",
            "secondary": rg.get("secondary-types") or [],
        })
    out.sort(key=lambda r: (r["year"] is None, r["year"] or 0, r["title"]))
    return out


def is_studio_album(rg: dict[str, Any]) -> bool:
    return rg.get("primary") == "Album" and not (set(rg.get("secondary", [])) & _NON_ALBUM_SECONDARY)


def search_recordings(artist: str, title: str, *, db: Database | None = None) -> list[dict[str, Any]]:
    """Artist-verified recording matches for ``artist``/``title``, best score first.

    Public wrapper over the internal search used for ISRC/canonical lookups —
    the track page walks these candidates looking for one with performer credits.
    """
    recs = [r for r in _search_recordings(artist, title, db) if _artist_matches(artist, r)]
    recs.sort(key=lambda r: r.get("score", 0), reverse=True)
    return recs


def recording_lookup(mbid: str, *, db: Database | None = None) -> dict[str, Any]:
    """Recording lookup including artist relations (performers) and work relations."""
    key = _cache_key("musicbrainz", "recording", mbid)

    def fetch() -> Any:
        return _mb_get(
            f"{API_URL}/recording/{mbid}",
            {"fmt": "json", "inc": "artist-rels+work-rels+artist-credits"},
        )

    payload = _cached_call(db, key, fetch)
    return payload if isinstance(payload, dict) else {}


# MB relation types that denote *performing* on a recording (not writing/producing).
_PERFORMANCE_RELS = {"vocal", "instrument", "performer", "performing orchestra", "conductor"}


def performers_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Performers on a recording — ``[{name, mbid, roles}]`` — performance only.

    Composers, lyricists, producers, engineers and other non-performing credits
    (which arrive as ``work-rels`` or as ``type`` outside :data:`_PERFORMANCE_RELS`)
    are deliberately excluded: the track page shows *who played on it*.
    """
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for rel in payload.get("relations", []) or []:
        if not isinstance(rel, dict) or rel.get("type") not in _PERFORMANCE_RELS:
            continue
        artist = rel.get("artist") or {}
        mbid = artist.get("id") or artist.get("name") or ""
        if not mbid:
            continue
        entry = grouped.get(mbid)
        if entry is None:
            entry = {"name": artist.get("name", ""), "mbid": artist.get("id"), "roles": []}
            grouped[mbid] = entry
            order.append(mbid)
        rtype = rel.get("type")
        attrs = [a for a in (rel.get("attributes") or []) if a]
        role = ", ".join(attrs) if attrs else ("vocals" if rtype == "vocal" else rtype)
        if role and role not in entry["roles"]:
            entry["roles"].append(role)
    return [grouped[m] for m in order]
