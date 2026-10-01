"""Lidarr integration — "Get with Lidarr" acquisition requests.

Lidarr (https://lidarr.audio) is the *arr music collection manager: it monitors
artists/albums and grabs releases through your download clients. Harmony doesn't
download anything itself — it hands Lidarr a request ("get this album/artist")
and Lidarr does the acquisition, filing the result into the music folder that the
local library (``harmony.library``) indexes and serves across the mesh.

The loop back is Lidarr's Connect → Webhook: :meth:`LidarrClient.register_webhook`
points it at this instance, and :func:`webhook_paths` turns its import/rename/
delete events into the files and folders to (re)index. :meth:`LidarrClient.queue`
and :meth:`LidarrClient.albums` report what's downloading and what's wanted, so
an album can show "downloading 40%" / "wanted" / "in your library".

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

#: Name of the Connect → Webhook entry Harmony registers in Lidarr.
WEBHOOK_NAME = "Harmony"
#: Lidarr notification event flags Harmony wants (imports, upgrades, renames,
#: retags and deletions all change what's on disk; grabs show "downloading").
WEBHOOK_EVENTS = ("onGrab", "onReleaseImport", "onUpgrade", "onRename", "onTrackRetag",
                  "onArtistDelete", "onAlbumDelete", "onDownloadFailure", "onImportFailure")


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

    def albums(self) -> list[dict[str, Any]]:
        """Every album Lidarr tracks (with ``statistics`` + ``monitored``)."""
        return self._req("GET", "album", params={"includeAllArtistAlbums": "false"}) or []

    def queue(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        """Active downloads (``/queue``), with their album + artist attached."""
        data = self._req("GET", "queue", params={
            "page": 1, "pageSize": page_size, "includeUnknownArtistItems": "false",
            "includeArtist": "true", "includeAlbum": "true"})
        if isinstance(data, dict):  # paged (current Lidarr)
            return list(data.get("records") or [])
        return list(data or [])

    # -- Connect → Webhook (the import callback into Harmony) ---------------

    def notifications(self) -> list[dict[str, Any]]:
        return self._req("GET", "notification") or []

    def register_webhook(self, url: str, *, name: str = WEBHOOK_NAME) -> dict[str, Any]:
        """Create or update a Webhook connection named ``name`` pointing at ``url``.

        Built from Lidarr's own Webhook schema so every event flag it supports is
        set without guessing field names per Lidarr version. Idempotent: an
        existing connection with the same name is updated in place.
        """
        schemas = self._req("GET", "notification/schema") or []
        schema = next((x for x in schemas if x.get("implementation") == "Webhook"), None)
        if schema is None:
            raise LidarrError("This Lidarr has no Webhook connection type.")
        existing = next((n for n in self.notifications()
                         if n.get("name") == name and n.get("implementation") == "Webhook"), None)
        payload = dict(existing or schema)
        payload["name"] = name
        payload["implementation"] = "Webhook"
        payload["configContract"] = schema.get("configContract", "WebhookSettings")
        for flag in WEBHOOK_EVENTS:
            supported = schema.get("supports" + flag[0].upper() + flag[1:])
            if supported is not False and flag in schema:
                payload[flag] = True
        fields = []
        for f in (existing or schema).get("fields") or []:
            f = dict(f)
            if f.get("name") == "url":
                f["value"] = url
            elif f.get("name") == "method":
                f["value"] = 1  # POST
            fields.append(f)
        payload["fields"] = fields
        payload.setdefault("tags", [])
        if existing:
            return self._req("PUT", f"notification/{existing['id']}", json=payload) or payload
        return self._req("POST", "notification", json=payload) or payload

    def test_webhook(self, notification: dict[str, Any]) -> None:
        """Ask Lidarr to fire its Test event at the registered webhook."""
        self._req("POST", "notification/test", json=notification)

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


# --------------------------------------------------------------------------
# Webhook payloads → what to (re)index
# --------------------------------------------------------------------------


def webhook_paths(payload: dict[str, Any]) -> dict[str, Any]:
    """What a Lidarr webhook event means for the library index.

    Returns ``{"event", "files", "deleted", "dirs"}``: audio files to (re)index,
    files to drop, and folders to rescan (a rescan also prunes what's gone).
    Paths are as *Lidarr* sees them; map them with
    :func:`harmony.library.map_path`. Lenient about payload shape — Lidarr's
    events differ per version (``Download`` carries ``trackFiles``; ``Rename``/
    ``Retag``/``ArtistDelete`` carry only the artist folder; ``AlbumDelete``
    sometimes neither, so the artist folder is the fallback).
    """
    event = str(payload.get("eventType") or "")
    files: list[str] = []
    deleted: list[str] = []
    dirs: list[str] = []

    def paths_of(items: Any) -> list[str]:
        out = []
        for it in items or []:
            if isinstance(it, dict) and it.get("path"):
                out.append(str(it["path"]))
        return out

    artist_dir = str((payload.get("artist") or {}).get("path") or "")
    if event in ("Download", "AlbumDownload", "TrackRetag", "Retag"):
        files += paths_of(payload.get("trackFiles"))
        if isinstance(payload.get("trackFile"), dict) and payload["trackFile"].get("path"):
            files.append(str(payload["trackFile"]["path"]))
        deleted += paths_of(payload.get("deletedFiles"))
        if not files and artist_dir:
            dirs.append(artist_dir)
    elif event == "Rename":
        renamed = payload.get("renamedTrackFiles") or []
        files += paths_of(renamed)
        deleted += [str(r["previousPath"]) for r in renamed
                    if isinstance(r, dict) and r.get("previousPath")]
        if not renamed and artist_dir:
            dirs.append(artist_dir)
    elif event in ("ArtistDelete", "AlbumDelete", "TrackFileDelete"):
        deleted += paths_of(payload.get("deletedFiles"))
        if isinstance(payload.get("trackFile"), dict) and payload["trackFile"].get("path"):
            deleted.append(str(payload["trackFile"]["path"]))
        album_dir = str((payload.get("album") or {}).get("path") or "")
        if album_dir:
            dirs.append(album_dir)
        elif artist_dir:
            dirs.append(artist_dir)
    return {"event": event, "files": files, "deleted": deleted, "dirs": dirs}


def queue_item(rec: dict[str, Any]) -> dict[str, Any]:
    """One ``/queue`` record → the fields a client shows for a download."""
    album = rec.get("album") or {}
    artist = rec.get("artist") or album.get("artist") or {}
    size = float(rec.get("size") or 0)
    left = float(rec.get("sizeleft") or 0)
    progress = round(100.0 * (size - left) / size, 1) if size > 0 else 0.0
    messages = [m for sm in rec.get("statusMessages") or []
                for m in (sm.get("messages") or []) if m]
    return {
        "id": rec.get("id"),
        "title": album.get("title") or rec.get("title") or "",
        "artist": artist.get("artistName") or "",
        "album_mbid": album.get("foreignAlbumId"),
        "album_id": rec.get("albumId") or album.get("id"),
        "status": str(rec.get("status") or "").lower(),
        "state": str(rec.get("trackedDownloadState") or "").lower(),
        "health": str(rec.get("trackedDownloadStatus") or "").lower(),
        "progress": progress,
        "size": int(size),
        "timeleft": rec.get("timeleft"),
        "protocol": rec.get("protocol"),
        "client": rec.get("downloadClient"),
        "release": rec.get("title"),
        "error": rec.get("errorMessage") or (messages[0] if messages else None),
    }
