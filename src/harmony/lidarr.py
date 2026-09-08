"""Lidarr integration — "Get with Lidarr" acquisition requests.

Lidarr (https://lidarr.audio) is the *arr music collection manager: it monitors
artists/albums and grabs releases through your download clients. Harmony doesn't
download anything itself — it hands Lidarr a request ("get this album/artist")
and Lidarr does the acquisition, filing the result into the music folder that the
planned local-library provider then serves across the mesh.

This is a thin REST client over Lidarr's ``/api/v1`` (key via ``X-Api-Key``) plus
the small amount of orchestration a one-click "get" needs: resolve the target in
Lidarr's own MusicBrainz-backed lookup, then add + monitor + search it. When
Harmony already knows a MusicBrainz id (from the entity layer), the match is exact
(``foreignAlbumId``/``foreignArtistId``); otherwise it falls back to a fuzzy
title/artist match. Engine-layer, gi-free, ``requests``-only.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from rapidfuzz import fuzz

from .errors import HarmonyError

log = logging.getLogger(__name__)

_MATCH_THRESHOLD = 78.0


class LidarrError(HarmonyError):
    """A Lidarr request failed (unreachable, bad key, or no match)."""


class LidarrClient:
    """Blocking REST client for one Lidarr instance."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 20) -> None:
        self.base = (base_url or "").rstrip("/")
        self.key = api_key or ""
        self.timeout = timeout

    def _req(self, method: str, path: str, *, params: dict[str, Any] | None = None,
             json: Any = None) -> Any:
        if not self.base or not self.key:
            raise LidarrError("Lidarr isn't configured (set its URL and API key first).")
        url = f"{self.base}/api/v1/{path.lstrip('/')}"
        try:
            r = requests.request(method, url, headers={"X-Api-Key": self.key},
                                 params=params, json=json, timeout=self.timeout)
        except requests.RequestException as exc:
            raise LidarrError(f"Couldn't reach Lidarr at {self.base}: {exc}") from None
        if r.status_code in (401, 403):
            raise LidarrError("Lidarr rejected the API key.")
        if not r.ok:
            raise LidarrError(f"Lidarr {method} {path} failed: HTTP {r.status_code} {r.text[:160]}")
        return r.json() if r.content else None

    # -- reads (also used to populate the settings form) --------------------

    def status(self) -> dict[str, Any]:
        """``/system/status`` — a quick connectivity + version probe."""
        return self._req("GET", "system/status")

    def root_folders(self) -> list[dict[str, Any]]:
        return self._req("GET", "rootfolder") or []

    def quality_profiles(self) -> list[dict[str, Any]]:
        return self._req("GET", "qualityprofile") or []

    def metadata_profiles(self) -> list[dict[str, Any]]:
        return self._req("GET", "metadataprofile") or []

    def lookup_album(self, term: str) -> list[dict[str, Any]]:
        return self._req("GET", "album/lookup", params={"term": term}) or []

    def lookup_artist(self, term: str) -> list[dict[str, Any]]:
        return self._req("GET", "artist/lookup", params={"term": term}) or []

    # -- the "get" orchestration -------------------------------------------

    def _defaults(self, root_folder: str, quality_profile_id: int | None,
                  metadata_profile_id: int | None) -> tuple[str, int, int]:
        """Fill any unset add-target from Lidarr's own first configured values."""
        root = root_folder or next((f.get("path", "") for f in self.root_folders()), "")
        if not root:
            raise LidarrError("Lidarr has no root folder configured to add music into.")
        qp = quality_profile_id or next((p.get("id") for p in self.quality_profiles()), None)
        mp = metadata_profile_id or next((p.get("id") for p in self.metadata_profiles()), None)
        if qp is None or mp is None:
            raise LidarrError("Lidarr has no quality/metadata profile to add against.")
        return root, int(qp), int(mp)

    def add_album(self, title: str, artist: str, *, mbid: str | None,
                  root_folder: str = "", quality_profile_id: int | None = None,
                  metadata_profile_id: int | None = None, search: bool = True) -> dict[str, Any]:
        """Add + monitor + (optionally) search an album, adding its artist if new.

        ``mbid`` is the MusicBrainz *release-group* id when known — it makes the
        match exact against Lidarr's ``foreignAlbumId``. Returns the created album.
        """
        term = f"{artist} {title}".strip() or title
        candidates = self.lookup_album(term)
        pick = _best_album(candidates, title, artist, mbid)
        if pick is None:
            raise LidarrError(f"Lidarr couldn't find “{title}”{f' by {artist}' if artist else ''}.")
        root, qp, mp = self._defaults(root_folder, quality_profile_id, metadata_profile_id)

        payload = dict(pick)
        payload["monitored"] = True
        payload["addOptions"] = {"searchForNewAlbum": bool(search)}
        art = dict(payload.get("artist") or {})
        art.update({"qualityProfileId": qp, "metadataProfileId": mp,
                    "rootFolderPath": root, "monitored": True})
        # Don't also blanket-search the whole artist; we search this album only.
        art["addOptions"] = {"monitor": "none", "searchForMissingAlbums": False}
        payload["artist"] = art
        return self._req("POST", "album", json=payload)

    def add_artist(self, name: str, *, mbid: str | None, root_folder: str = "",
                   quality_profile_id: int | None = None, metadata_profile_id: int | None = None,
                   search: bool = True) -> dict[str, Any]:
        """Add + monitor an artist (and search for its albums)."""
        candidates = self.lookup_artist(name)
        pick = _best_artist(candidates, name, mbid)
        if pick is None:
            raise LidarrError(f"Lidarr couldn't find the artist “{name}”.")
        root, qp, mp = self._defaults(root_folder, quality_profile_id, metadata_profile_id)

        payload = dict(pick)
        payload.update({"qualityProfileId": qp, "metadataProfileId": mp,
                        "rootFolderPath": root, "monitored": True})
        payload["addOptions"] = {"monitor": "all", "searchForMissingAlbums": bool(search)}
        return self._req("POST", "artist", json=payload)


def _best_album(candidates: list[dict[str, Any]], title: str, artist: str,
                mbid: str | None) -> dict[str, Any] | None:
    if mbid:
        for c in candidates:
            if c.get("foreignAlbumId") == mbid:
                return c
    best, best_score = None, 0.0
    for c in candidates:
        score = fuzz.token_sort_ratio(title.lower(), (c.get("title") or "").lower())
        if artist:
            score = 0.6 * score + 0.4 * fuzz.token_sort_ratio(
                artist.lower(), ((c.get("artist") or {}).get("artistName") or "").lower())
        if score > best_score and score >= _MATCH_THRESHOLD:
            best, best_score = c, score
    return best


def _best_artist(candidates: list[dict[str, Any]], name: str,
                 mbid: str | None) -> dict[str, Any] | None:
    if mbid:
        for c in candidates:
            if c.get("foreignArtistId") == mbid:
                return c
    best, best_score = None, 0.0
    for c in candidates:
        score = fuzz.token_sort_ratio(name.lower(), (c.get("artistName") or "").lower())
        if score > best_score and score >= _MATCH_THRESHOLD:
            best, best_score = c, score
    return best
