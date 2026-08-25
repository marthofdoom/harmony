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

#: Lazily-resolved, process-wide cache for ``config.user_agent()``'s return
#: value. ``_get_json`` runs on worker threads (never the GTK main loop), so
#: the disk read behind a no-arg ``user_agent()`` call was never a main-loop
#: violation -- but every enrichment HTTP call re-reading settings.json just
#: to rebuild the same header string is still needless I/O per request.
#: Populated on first use rather than at import time (an import-time disk
#: read would run before ``AppState`` has even loaded ``Settings``, and could
#: fire in contexts — like a future headless/web port — that never touch the
#: user's config dir at all). ``reset_user_agent_cache()`` invalidates it
#: after a settings change so a new contact email takes effect on the next
#: call rather than being stuck at whatever was cached first.
_user_agent_cache: str | None = None


def _cached_user_agent() -> str:
    global _user_agent_cache
    if _user_agent_cache is None:
        _user_agent_cache = config.user_agent()
    return _user_agent_cache


def reset_user_agent_cache() -> None:
    """Invalidate the cached User-Agent so the next request rebuilds it.

    Call after anything that changes ``Settings.contact_email`` (e.g.
    Preferences saving a new one) so enrichment requests pick up the new
    value instead of the one resolved on first use.
    """
    global _user_agent_cache
    _user_agent_cache = None


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
    merged_headers = {"User-Agent": _cached_user_agent()}
    if headers:
        merged_headers.update(headers)
    try:
        response = requests.get(url, params=params, headers=merged_headers, timeout=timeout)
    except requests.RequestException as exc:
        # requests/urllib3 embed the full request URL - including any secret
        # query params (e.g. Last.fm's api_key) - in their own exception
        # text, which we'd otherwise be re-raising verbatim into a message
        # that gets logged (and shown in the UI, for callers that surface it).
        # Redacting only *this* message isn't enough: chaining "from exc"
        # keeps the raw, unredacted exception reachable as __cause__, and
        # anything that renders the full chain (tasks.run_async's
        # log.exception, a bare traceback) prints it verbatim regardless of
        # what our own message says. Chain from a redacted stand-in instead.
        redacted = config.redact_secrets(str(exc))
        raise ProviderError(f"Request to {url} failed: {redacted}") from config.redact_exception(exc)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimitedError(
            f"Rate limited by {url}",
            retry_after=float(retry_after) if retry_after else None,
        )
    if not response.ok:
        excerpt = config.redact_secrets(response.text[:200])
        raise ProviderError(f"{url} returned HTTP {response.status_code}: {excerpt}")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(f"{url} returned non-JSON response") from exc


def _cache_key(*parts: str) -> str:
    """Build a stable ``module:kind:args`` cache key from ``parts``."""
    return ":".join(parts)


def _cached_call(db: Any | None, key: str, fetch: Any, *, cache_empty: bool = True) -> Any:
    """Return ``db.cache_get(key)`` if fresh, else call ``fetch()`` and store it.

    ``db`` is duck-typed (``harmony.db.Database`` at runtime) so this module
    never has to import it. ``fetch`` must return a JSON-serialisable value.

    ``cache_empty=False`` skips writing a falsy result (``[]``, ``{}``,
    ``None``, ...) to the cache. Use it for sources where "no data right now"
    is a routine, possibly-transient outcome (a coverage gap, a momentary
    upstream hiccup) rather than a stable fact — otherwise a single empty
    response gets pinned in the cache for the full :data:`CACHE_TTL_S` and
    silently disables the source for that long.
    """
    if db is not None:
        cached = db.cache_get(key, max_age_s=CACHE_TTL_S)
        if cached is not None:
            return cached
    value = fetch()
    if db is not None and (cache_empty or value):
        db.cache_put(key, value)
    return value
