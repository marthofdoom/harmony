"""ListenBrainz enrichment: open, no-key-required recommendation endpoints.

ListenBrainz coverage is patchy by nature (community-contributed, sparse for
niche artists), so a missing recommendation is a normal outcome here, not a
failure — both public functions degrade to an empty list (logged at debug)
when the endpoint 404s or the service is unreachable, rather than raising.

``similar_recordings`` hits the "labs" dataset-hoster API
(``labs.api.listenbrainz.org``), not ``api.listenbrainz.org/1`` -- the latter
has no ``/similar-recordings`` route at all (confirmed live: every algorithm,
valid or not, 404s there). This was verified against the real service on
2026-08-25: ``GET
https://labs.api.listenbrainz.org/similar-recordings/json?recording_mbids=<mbid>&algorithm=<bad>``
400s with a Pydantic enum error listing the exact permitted algorithm
strings, and a real popular-recording mbid against a valid algorithm returns
a JSON list of ``{recording_mbid, recording_name, artist_credit_name,
artist_credit_mbids, release_name, release_mbid, caa_id, caa_release_mbid,
score, reference_mbid}`` dicts.
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

# ``similar_recordings`` lives on a different host entirely -- the "labs"
# dataset-hoster, not the main API -- while ``radio_from_artist``'s
# ``explore/lb-radio`` route is a real endpoint on ``API_URL`` (confirmed
# live: it 401s rather than 404s, i.e. the route exists) so that one is left
# alone.
LABS_API_URL = "https://labs.api.listenbrainz.org"

# One of the 7 values ListenBrainz's labs API actually permits (confirmed
# live via the enum listed in its 400 response). The old value here had a
# stray "_filter_True" segment that isn't in the enum at all, so every call
# 400'd even before the host/path were fixed. Named separately so the
# algorithm can be swapped without touching call sites — ListenBrainz
# occasionally retires/renames these presets.
SIMILAR_RECORDINGS_ALGORITHM = "session_based_days_7500_session_300_contribution_5_threshold_15_limit_50_skip_30"


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
        raise ProviderError(f"{url} returned HTTP {response.status_code}: {config.redact_secrets(response.text[:200])}")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(f"{url} returned non-JSON response") from exc


def _normalize_similar_recording(raw: dict[str, Any]) -> dict[str, Any]:
    """Map the labs API's real field names onto the shape the rest of the app
    (``recommender.py``) expects: ``recording_name``/``artist_name``/``score``.

    The live response uses ``artist_credit_name`` rather than ``artist_name``
    (see the module docstring for the full verified shape), so this is where
    that gets translated -- keeping the public return shape stable for
    callers regardless of what the upstream API happens to name things.
    """
    return {
        "recording_mbid": raw.get("recording_mbid"),
        "recording_name": raw.get("recording_name"),
        "artist_name": raw.get("artist_credit_name"),
        "release_name": raw.get("release_name"),
        "score": raw.get("score"),
    }


def similar_recordings(
    mbid: str, *, limit: int = 30, db: Database | None = None
) -> list[dict[str, Any]]:
    """Recordings ListenBrainz's session-based model considers similar to ``mbid``."""
    key = _cache_key("listenbrainz", "similar_recordings", f"{mbid}|{limit}|{SIMILAR_RECORDINGS_ALGORITHM}")

    def fetch() -> Any:
        url = f"{LABS_API_URL}/similar-recordings/json"
        params = {"recording_mbids": mbid, "algorithm": SIMILAR_RECORDINGS_ALGORITHM}
        result = _get_optional_json(url, params)
        return result if result is not None else []

    # cache_empty=False: "no similar recordings for this mbid" is often just
    # a transient coverage gap, not a durable fact -- don't pin an empty
    # result in the cache for a full week (CACHE_TTL_S) and silently disable
    # this source for that long.
    payload = _cached_call(db, key, fetch, cache_empty=False)
    if not isinstance(payload, list):
        return []
    return [_normalize_similar_recording(r) for r in payload[:limit] if isinstance(r, dict)]


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

    payload = _cached_call(db, key, fetch, cache_empty=False)
    if not isinstance(payload, dict):
        return []
    tracks = payload.get("payload", {}).get("jspf", {}).get("playlist", {}).get("track", [])
    if not isinstance(tracks, list):
        return []
    return tracks[:limit]
