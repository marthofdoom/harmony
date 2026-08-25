"""ListenBrainz enrichment: open, no-key-required recommendation endpoints.

ListenBrainz coverage is patchy by nature (community-contributed, sparse for
niche artists), so a missing recommendation is a normal outcome here, not a
failure — both public functions degrade to an empty list (logged at debug)
when the endpoint 404s or the service is unreachable, rather than raising.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import requests

from .. import config
from ..errors import ProviderError, RateLimitedError
from . import _cache_key, _cached_call

if TYPE_CHECKING:
    from ..db import Database

log = logging.getLogger(__name__)

API_URL = "https://api.listenbrainz.org/1"

# Named separately so the algorithm can be swapped without touching call
# sites — ListenBrainz occasionally retires/renames these presets.
SIMILAR_RECORDINGS_ALGORITHM = "session_based_days_7500_session_300_contribution_5_threshold_15_limit_50_filter_True_skip_30"


def _get_optional_json(url: str, params: dict[str, Any] | None = None) -> Any | None:
    """GET ``url``, returning ``None`` (logged at debug) on 404 or unreachable.

    Other failures (rate limiting, 5xx) still raise — those are actionable
    problems, unlike "this recording just isn't in ListenBrainz's graph".
    """
    headers = {"User-Agent": config.user_agent()}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
    except requests.RequestException as exc:
        log.debug("ListenBrainz unreachable at %s: %s", url, exc)
        return None
    if response.status_code == 404:
        log.debug("ListenBrainz has no data at %s", url)
        return None
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimitedError(
            f"Rate limited by {url}", retry_after=float(retry_after) if retry_after else None
        )
    if not response.ok:
        raise ProviderError(f"{url} returned HTTP {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(f"{url} returned non-JSON response") from exc


def similar_recordings(
    mbid: str, *, limit: int = 30, db: Database | None = None
) -> list[dict[str, Any]]:
    """Recordings ListenBrainz's session-based model considers similar to ``mbid``."""
    key = _cache_key("listenbrainz", "similar_recordings", f"{mbid}|{limit}")

    def fetch() -> Any:
        url = f"{API_URL}/similar-recordings/{mbid}/{SIMILAR_RECORDINGS_ALGORITHM}"
        result = _get_optional_json(url)
        return result if result is not None else []

    payload = _cached_call(db, key, fetch)
    if not isinstance(payload, list):
        return []
    return payload[:limit]


def radio_from_artist(
    artist_mbid: str, *, limit: int = 30, db: Database | None = None
) -> list[dict[str, Any]]:
    """Tracks from the LB Radio "artist" prompt seeded on ``artist_mbid``."""
    key = _cache_key("listenbrainz", "lb_radio", f"{artist_mbid}|{limit}")

    def fetch() -> Any:
        url = f"{API_URL}/explore/lb-radio"
        params = {"prompt": f"artist:({artist_mbid})", "mode": "easy"}
        result = _get_optional_json(url, params)
        return result if result is not None else {}

    payload = _cached_call(db, key, fetch)
    if not isinstance(payload, dict):
        return []
    tracks = payload.get("payload", {}).get("jspf", {}).get("playlist", {}).get("track", [])
    if not isinstance(tracks, list):
        return []
    return tracks[:limit]
