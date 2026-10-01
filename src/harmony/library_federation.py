"""Federated library — every instance's music folders as ONE "Library" service.

Each Harmony instance can index its own folders (``harmony.library``). The
mesh makes them one: the ``local`` ("Library") service an instance exposes is
its own library *plus* the libraries of its key-matching peers. So the desktop
app, the phone and the web client all search, browse and play the server's
library (and any other instance's) without being pointed at that server.

Ids say where an item lives: a bare id is this instance's own; ``<id>@host:port``
is the peer at that address. A peer is asked through its *own-only* endpoints
(``/api/library/own/...``), never its federated view, so federation can't
recurse. Playback resolves on the owning peer and streams from its ``/stream/``
URL with our personal key — the web proxy, the cast relay and the desktop
player already play HTTP sources. Only peers the mesh knows (discovered or
saved) are contacted, so an id can't point the server at an arbitrary host.

Engine-layer, gi-free.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

from .errors import NotSupportedError, ProviderError
from .models import Album, Artist, Playlist, SearchResults, Service, StreamSource, Track
from .providers.base import MusicProvider

log = logging.getLogger(__name__)

#: How long a peer's "has a library" probe is trusted.
_PROBE_TTL_S = 60.0
#: Per-request budget for a peer; a slow/absent peer just contributes nothing.
_PEER_TIMEOUT_S = 4.0


def fed_id(raw: str, peer: str) -> str:
    """``raw`` (a peer's own id) addressed through the peer ``host:port``."""
    return f"{raw}@{peer}"


def split_id(item_id: str) -> tuple[str, str | None]:
    """``"<id>@host:port"`` → ``(id, "host:port")``; a bare id → ``(id, None)``."""
    raw, sep, peer = (item_id or "").rpartition("@")
    if sep and raw and peer:
        return raw, peer
    return item_id, None


def _base(peer: str) -> str:
    host, _, port = peer.rpartition(":")
    host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{host}:{port}"


class FederatedLibraryProvider(MusicProvider):
    """The Library service: this instance's library + its peers' libraries.

    ``own`` is this instance's :class:`LocalLibraryProvider` (``None`` when it
    has no library of its own); ``peers`` returns the mesh's instances
    (``[{"host", "port", "name"}, ...]``); ``key`` returns the personal key.
    """

    service = Service.LOCAL

    def __init__(self, own: Any | None, peers: Callable[[], list[dict[str, Any]]],
                 key: Callable[[], str | None]) -> None:
        self.own = own
        self._peers = peers
        self._key = key
        self._probe: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._probe_lock = threading.Lock()

    # -- peers -----------------------------------------------------------------

    def _known(self) -> dict[str, str]:
        """``{"host:port": name}`` for every instance the mesh knows."""
        out: dict[str, str] = {}
        try:
            for p in self._peers() or []:
                if p.get("host") and p.get("port"):
                    out[f"{p['host']}:{p['port']}"] = str(p.get("name") or p["host"])
        except Exception as exc:  # noqa: BLE001 - no mesh means no peers
            log.debug("library federation: listing peers failed: %s", exc)
        return out

    def _get(self, peer: str, path: str, params: dict[str, Any] | None = None,
             timeout: float = _PEER_TIMEOUT_S) -> Any:
        import requests

        key = self._key()
        headers = {"X-Harmony-Key": key} if key else {}
        r = requests.get(_base(peer) + path, params=params, headers=headers, timeout=timeout)
        if r.status_code == 404:
            raise ProviderError("That isn't in the peer's library any more.")
        if not r.ok:
            raise ProviderError(f"Library peer {peer} answered HTTP {r.status_code}.")
        return r.json()

    def _status(self, peer: str) -> dict[str, Any] | None:
        """The peer's own-library status (cached), or None if it has none/is down."""
        now = time.monotonic()
        with self._probe_lock:
            hit = self._probe.get(peer)
            if hit and now - hit[0] < _PROBE_TTL_S:
                return hit[1]
        try:
            st = self._get(peer, "/api/library/own/status", timeout=2.5)
            st = st if st.get("enabled") and (st.get("stats") or {}).get("tracks") else None
        except Exception as exc:  # noqa: BLE001 - absent/old/foreign peer
            log.debug("library federation: %s has no library for us (%s)", peer, exc)
            st = None
        with self._probe_lock:
            self._probe[peer] = (now, st)
        return st

    def library_peers(self) -> list[dict[str, Any]]:
        """Peers that currently share a library: ``[{peer, name, stats}]``."""
        known = self._known()
        if not known:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(known))) as pool:
            statuses = list(pool.map(self._status, known))
        return [{"peer": peer, "name": known[peer], "stats": st["stats"]}
                for peer, st in zip(known, statuses, strict=True) if st]

    def _fan_out(self, fn: Callable[[str], Any]) -> list[tuple[str, Any]]:
        """Run ``fn(peer)`` on every library peer in parallel; failures drop out."""
        peers = [p["peer"] for p in self.library_peers()]
        if not peers:
            return []

        def safe(peer: str) -> Any:
            try:
                return fn(peer)
            except Exception as exc:  # noqa: BLE001 - one peer mustn't break the rest
                log.info("library federation: %s failed: %s", peer, exc)
                return None

        with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
            results = list(pool.map(safe, peers))
        return [(p, r) for p, r in zip(peers, results, strict=True) if r is not None]

    def _route(self, item_id: str) -> tuple[str, str | None]:
        raw, peer = split_id(item_id)
        if peer is not None and peer not in self._known():
            raise ProviderError(f"Library peer {peer} isn't a known instance.")
        return raw, peer

    def _own(self) -> Any:
        if self.own is None:
            raise ProviderError("This instance has no library of its own.")
        return self.own

    # -- peer JSON → models (ids re-addressed through the peer) -----------------

    @staticmethod
    def _art(peer: str, url: Any) -> str | None:
        if isinstance(url, str) and url.startswith("/"):
            return _base(peer) + url  # /art/ paths are key-free capability URLs
        return url or None

    def _track(self, peer: str, d: dict[str, Any]) -> Track:
        artist = d.get("artist") or ""
        return Track(
            id=fed_id(str(d["id"]), peer), title=d.get("title") or "", service=Service.LOCAL,
            artists=[a.strip() for a in artist.split(",")] if artist else [],
            artist_ids=[fed_id(a, peer) for a in d.get("artist_ids") or []],
            album=d.get("album"),
            album_id=fed_id(d["album_id"], peer) if d.get("album_id") else None,
            duration_s=d.get("duration_s"), track_number=d.get("track_number"),
            year=d.get("year"), isrc=d.get("isrc"),
            artwork_url=self._art(peer, d.get("artwork_url")),
            raw={"peer": peer},
        )

    def _album(self, peer: str, d: dict[str, Any]) -> Album:
        artist = d.get("artist") or ""
        return Album(
            id=fed_id(str(d["id"]), peer), title=d.get("title") or "", service=Service.LOCAL,
            artists=[artist] if artist else [],
            artist_ids=[fed_id(a, peer) for a in d.get("artist_ids") or []],
            year=d.get("year"), date=d.get("date"), track_count=d.get("track_count"),
            artwork_url=self._art(peer, d.get("artwork_url")), raw={"peer": peer},
        )

    def _artist(self, peer: str, d: dict[str, Any]) -> Artist:
        return Artist(id=fed_id(str(d["id"]), peer), name=d.get("name") or "",
                      service=Service.LOCAL, raw={"peer": peer})

    # -- MusicProvider -----------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return True

    def authenticate(self) -> None:
        return None

    def active(self) -> bool:
        """Whether there's any library to show (own or a peer's)."""
        return self.own is not None or bool(self.library_peers())

    def account_name(self) -> str | None:
        n = self.own.index.stats()["tracks"] if self.own is not None else 0
        peers = self.library_peers()
        n += sum(int((p["stats"] or {}).get("tracks") or 0) for p in peers)
        text = f"{n} track{'s' if n != 1 else ''}"
        return text + (f" · {len(peers)} shared" if peers else "")

    def search(self, query: str, *, kinds: Sequence[str] = ("tracks",),
               limit: int = 25) -> SearchResults:
        out = self.own.search(query, kinds=kinds, limit=limit) if self.own else SearchResults()
        params = {"q": query, "kinds": ",".join(kinds), "limit": limit}
        for peer, r in self._fan_out(lambda p: self._get(p, "/api/library/own/search", params)):
            out.tracks += [self._track(peer, t) for t in r.get("tracks") or []]
            out.albums += [self._album(peer, a) for a in r.get("albums") or []]
            out.artists += [self._artist(peer, a) for a in r.get("artists") or []]
        return out

    def get_track(self, track_id: str) -> Track:
        raw, peer = self._route(track_id)
        if peer is None:
            return self._own().get_track(raw)
        return self._track(peer, self._get(peer, f"/api/library/own/track/{quote(raw, safe='')}")["track"])

    def _album_page(self, raw: str, peer: str) -> dict[str, Any]:
        return self._get(peer, f"/api/library/own/album/{quote(raw, safe='')}")

    def get_album_detail(self, album_id: str) -> Album:
        raw, peer = self._route(album_id)
        if peer is None:
            return self._own().get_album_detail(raw)
        return self._album(peer, self._album_page(raw, peer)["album"])

    def get_album_tracks(self, album_id: str) -> list[Track]:
        raw, peer = self._route(album_id)
        if peer is None:
            return self._own().get_album_tracks(raw)
        return [self._track(peer, t) for t in self._album_page(raw, peer).get("tracks") or []]

    def _artist_page(self, raw: str, peer: str) -> dict[str, Any]:
        return self._get(peer, f"/api/library/own/artist/{quote(raw, safe='')}")

    def get_artist_detail(self, artist_id: str) -> Artist:
        raw, peer = self._route(artist_id)
        if peer is None:
            return self._own().get_artist_detail(raw)
        return self._artist(peer, self._artist_page(raw, peer)["artist"])

    def get_artist_albums(self, artist_id: str, *, limit: int = 100) -> list[Album]:
        raw, peer = self._route(artist_id)
        if peer is None:
            return self._own().get_artist_albums(raw, limit=limit)
        return [self._album(peer, a) for a in self._artist_page(raw, peer).get("albums") or []][:limit]

    def get_artist_top_tracks(self, artist_id: str, *, limit: int = 20) -> list[Track]:
        raw, peer = self._route(artist_id)
        if peer is None:
            return self._own().get_artist_top_tracks(raw, limit=limit)
        return [self._track(peer, t) for t in self._artist_page(raw, peer).get("top_tracks") or []][:limit]

    def browse(self, *, order: str = "recent", limit: int = 60) -> tuple[list[Album], list[dict[str, Any]]]:
        """Albums + artists across every library (own first, then each peer's)."""
        albums: list[Album] = []
        artists: list[dict[str, Any]] = []
        if self.own is not None:
            albums += self.own.list_albums(order=order, limit=limit)
            artists += [{"id": a["id"], "name": a["name"], "service": "local",
                         "album_count": a["album_count"]} for a in self.own.list_artists()]
        params = {"order": order, "limit": limit}
        for peer, r in self._fan_out(lambda p: self._get(p, "/api/library/own/browse", params)):
            albums += [self._album(peer, a) for a in r.get("albums") or []]
            artists += [{**a, "id": fed_id(str(a["id"]), peer), "service": "local"}
                        for a in r.get("artists") or []]
        return albums, artists

    def match(self, items: list[dict[str, Any]]) -> list[str | None]:
        """Library copies of streaming tracks (``[{title, artist, isrc,
        duration_s}]``): this instance's own first, then one batched request
        per peer for the rest. Returns Library ids (``id`` / ``id@peer``)."""
        found: list[str | None] = [None] * len(items)
        if self.own is not None:
            found = list(self.own.index.match_tracks(items))
        missing = [i for i, f in enumerate(found) if f is None]
        if not missing:
            return found
        want = [{k: items[i].get(k) for k in ("title", "artist", "isrc", "duration_s")} for i in missing]

        def ask(peer: str) -> Any:
            import requests

            key = self._key()
            r = requests.post(_base(peer) + "/api/library/own/match", json={"tracks": want},
                              headers={"X-Harmony-Key": key} if key else {}, timeout=_PEER_TIMEOUT_S)
            return r.json().get("ids") if r.ok else None

        for peer, ids in self._fan_out(ask):
            for i, raw in zip(missing, ids or [], strict=False):
                if raw and found[i] is None:
                    found[i] = fed_id(str(raw), peer)
        return found

    def resolve_stream(self, track_id: str, *, max_quality: bool = False) -> StreamSource:
        raw, peer = self._route(track_id)
        if peer is None:
            return self._own().resolve_stream(raw, max_quality=max_quality)
        r = self._get(peer, "/api/resolve", {"service": "local", "id": raw}, timeout=10)
        key = self._key()
        name = self._known().get(peer, peer)
        return StreamSource(url=f"{_base(peer)}/stream/{r['token']}", mime_type=r.get("mime") or "",
                            headers={"X-Harmony-Key": key} if key else {},
                            label=f"{r.get('label') or 'Library'} · {name}")

    # -- no playlists / social on a folder of files ---------------------------------

    def list_playlists(self) -> list[Playlist]:
        return []

    def get_playlist(self, playlist_id: str) -> Playlist:
        raise NotSupportedError("The library has no playlists.")

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        raise NotSupportedError("The library has no playlists.")

    def create_playlist(self, title: str, description: str = "", public: bool = False) -> Playlist:
        raise NotSupportedError("Playlists live on a streaming service, not the library.")

    def add_tracks(self, playlist_id: str, track_ids: Sequence[str]) -> None:
        raise NotSupportedError("The library has no playlists.")

    def remove_tracks(self, playlist_id: str, track_ids: Sequence[str]) -> None:
        raise NotSupportedError("The library has no playlists.")

    def delete_playlist(self, playlist_id: str) -> None:
        raise NotSupportedError("The library has no playlists.")

    def rename_playlist(self, playlist_id: str, title: str, description: str | None = None) -> None:
        raise NotSupportedError("The library has no playlists.")

    def similar_tracks(self, track: Track, *, limit: int = 20) -> list[Track]:
        return []

    def liked_tracks(self, *, limit: int = 500) -> list[Track]:
        return []


__all__ = ["FederatedLibraryProvider", "fed_id", "split_id"]
