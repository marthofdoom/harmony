"""Music-data enrichment: Last.fm, MusicBrainz, ListenBrainz, and blended recommendations.

Every module here is a set of stateless functions over plain strings. Network
calls go through the shared :func:`_get_json` helper so User-Agent handling,
rate limiting, and error translation live in exactly one place. Callers may
pass a ``db`` (duck-typed ``harmony.db.Database``) to cache responses for a
week — see :func:`_cached_call`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import requests

from .. import config
from ..errors import ProviderError, RateLimitedError

if TYPE_CHECKING:
    from ..tasks import RateLimiter

log = logging.getLogger(__name__)

#: All enrichment lookups are cached for a week — metadata like "similar
#: tracks" or "canonical release" changes rarely enough that this is a
#: reasonable staleness bound, and it keeps us well within API etiquette.
CACHE_TTL_S = 7 * 24 * 3600


def _get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    rate_limiter: RateLimiter | None = None,
    timeout: float = 15,
) -> Any:
    """GET ``url`` and parse the JSON body, translating transport failures.

    Centralised so every provider in this package gets the same User-Agent
    header, optional rate-limit wait, and error mapping without repeating
    the boilerplate three times.
    """
    if rate_limiter is not None:
        rate_limiter.wait()
    merged_headers = {"User-Agent": config.user_agent()}
    if headers:
        merged_headers.update(headers)
    try:
        response = requests.get(url, params=params, headers=merged_headers, timeout=timeout)
    except requests.RequestException as exc:
        raise ProviderError(f"Request to {url} failed: {exc}") from exc
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimitedError(
            f"Rate limited by {url}",
            retry_after=float(retry_after) if retry_after else None,
        )
    if not response.ok:
        raise ProviderError(f"{url} returned HTTP {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(f"{url} returned non-JSON response") from exc


def _cache_key(*parts: str) -> str:
    """Build a stable ``module:kind:args`` cache key from ``parts``."""
    return ":".join(parts)


def _cached_call(db: Any | None, key: str, fetch: Any) -> Any:
    """Return ``db.cache_get(key)`` if fresh, else call ``fetch()`` and store it.

    ``db`` is duck-typed (``harmony.db.Database`` at runtime) so this module
    never has to import it. ``fetch`` must return a JSON-serialisable value.
    """
    if db is not None:
        cached = db.cache_get(key, max_age_s=CACHE_TTL_S)
        if cached is not None:
            return cached
    value = fetch()
    if db is not None:
        db.cache_put(key, value)
    return value
