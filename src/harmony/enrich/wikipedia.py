"""Wikipedia/Wikidata bio extracts for artist and album detail pages.

MusicBrainz holds the *link* (a ``wikidata`` or ``wikipedia`` URL relation on the
artist) but not prose; this module turns that link into a short lead extract via
Wikipedia's REST summary API. Reached only through MusicBrainz's own relation, so
there is no fuzzy title-guessing — the identity is already pinned by MBID.

No API key. Same shared ``_get_json`` + one-week cache as the rest of ``enrich``.
A modest rate limiter keeps us polite to the WMF endpoints.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from ..tasks import RateLimiter
from . import _cache_key, _cached_call, _get_json

if TYPE_CHECKING:
    from ..db import Database

log = logging.getLogger(__name__)

_rate_limiter = RateLimiter(0.2)  # 5 req/s is well within WMF etiquette

_WIKIDATA_API = "https://www.wikidata.org/w/api.php"


def _parse_wikipedia_url(url: str) -> tuple[str, str] | None:
    """``https://en.wikipedia.org/wiki/Linkin_Park`` -> ``("en", "Linkin_Park")``."""
    marker = ".wikipedia.org/wiki/"
    if marker not in url:
        return None
    host, _, title = url.partition(marker)
    lang = host.rsplit("//", 1)[-1].split(".")[0] or "en"
    title = title.split("#")[0].strip()
    return (lang, title) if title else None


def _wikidata_id(url: str) -> str | None:
    """Extract ``Q…`` from a wikidata.org URL."""
    if "wikidata.org" not in url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail if tail.startswith("Q") and tail[1:].isdigit() else None


def _enwiki_title_from_wikidata(qid: str, *, db: Database | None) -> tuple[str, str] | None:
    """Resolve a Wikidata Q-id to a ``(lang, title)`` Wikipedia sitelink (English preferred)."""
    key = _cache_key("wikidata", "sitelinks", qid)

    def fetch() -> Any:
        return _get_json(
            _WIKIDATA_API,
            {"action": "wbgetentities", "ids": qid, "props": "sitelinks",
             "format": "json", "formatversion": "2"},
            rate_limiter=_rate_limiter,
        )

    payload = _cached_call(db, key, fetch)
    try:
        sitelinks = payload["entities"][qid]["sitelinks"]
    except (KeyError, TypeError):
        return None
    for wiki in ("enwiki", "simplewiki"):
        if wiki in sitelinks:
            title = sitelinks[wiki].get("title", "").replace(" ", "_")
            if title:
                return ("en", title)
    # Fall back to any available language sitelink.
    for name, link in sitelinks.items():
        if name.endswith("wiki") and not name.endswith("quotewiki"):
            title = link.get("title", "").replace(" ", "_")
            if title:
                return (name[:-4], title)
    return None


def _summary(lang: str, title: str, *, db: Database | None) -> dict[str, str] | None:
    key = _cache_key("wikipedia", "summary", f"{lang}:{title}")

    def fetch() -> Any:
        url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
        return _get_json(url, {}, rate_limiter=_rate_limiter)

    payload = _cached_call(db, key, fetch, cache_empty=False)
    if not isinstance(payload, dict):
        return None
    extract = (payload.get("extract") or "").strip()
    if not extract:
        return None
    page_url = (payload.get("content_urls", {}).get("desktop", {}).get("page")
                or f"https://{lang}.wikipedia.org/wiki/{title}")
    return {"text": extract, "url": page_url, "source": "wikipedia"}


def bio_from_urls(urls: dict[str, str], *, db: Database | None = None) -> dict[str, str] | None:
    """A short bio for whichever of MusicBrainz's URL relations resolves first.

    ``urls`` is the ``{rel_type: url}`` map from ``musicbrainz.artist_urls``.
    Prefers a direct ``wikipedia`` relation, else resolves ``wikidata`` to its
    English sitelink. Returns ``{text, url, source}`` or ``None``.
    """
    wp = urls.get("wikipedia")
    if wp:
        parsed = _parse_wikipedia_url(wp)
        if parsed:
            summary = _summary(parsed[0], unquote(parsed[1]), db=db)
            if summary:
                return summary
    wd = urls.get("wikidata")
    if wd:
        qid = _wikidata_id(wd)
        if qid:
            resolved = _enwiki_title_from_wikidata(qid, db=db)
            if resolved:
                return _summary(resolved[0], unquote(resolved[1]), db=db)
    return None
