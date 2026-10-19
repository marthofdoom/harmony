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

import heapq
import logging
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import cmp_to_key
from typing import Any
from urllib.parse import quote

from .errors import NotSupportedError, ProviderError
from .models import Album, Artist, Playlist, SearchResults, Service, StreamSource, Track
from .providers.base import MusicProvider

log = logging.getLogger(__name__)

#: How long a peer's "has a library" probe is trusted.
_PROBE_TTL_S = 60.0
#: Rows each library contributes to a merged (multi-library) browse page.
_MERGE_CAP = 5000
#: Per-request budget for a peer; a slow/absent peer just contributes nothing.
_PEER_TIMEOUT_S = 4.0


class PeerNotFound(ProviderError):
    """The peer answered 404 (an item gone, or an endpoint an older version lacks)."""


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
            raise PeerNotFound("That isn't in the peer's library any more.")
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

    def _library_peer_ids(self) -> list[str]:
        """``host:port`` of every peer currently sharing a library."""
        return [p["peer"] for p in self.library_peers()]

    def _fan_out(self, fn: Callable[[str], Any],
                 peers: list[str] | None = None) -> list[tuple[str, Any]]:
        """Run ``fn(peer)`` on every library peer (or ``peers``) in parallel;
        failures drop out."""
        peers = self._library_peer_ids() if peers is None else peers
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
            genre=d.get("genre"), quality=d.get("quality") if isinstance(d.get("quality"), dict) else None,
            raw={"peer": peer, **({"sort": d["sort"]} if isinstance(d.get("sort"), dict) else {})},
        )

    def _album(self, peer: str, d: dict[str, Any]) -> Album:
        artist = d.get("artist") or ""
        return Album(
            id=fed_id(str(d["id"]), peer), title=d.get("title") or "", service=Service.LOCAL,
            artists=[artist] if artist else [],
            artist_ids=[fed_id(a, peer) for a in d.get("artist_ids") or []],
            year=d.get("year"), date=d.get("date"), track_count=d.get("track_count"),
            artwork_url=self._art(peer, d.get("artwork_url")),
            genre=d.get("genre"), quality=d.get("quality") if isinstance(d.get("quality"), dict) else None,
            raw={"peer": peer, "added_at": d.get("added_at")},
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

    # -- iTunes-style browse, merged across every library ------------------------

    def browse_view(self, view: str, *, order: str = "artist", desc: bool = False,
                    limit: int = 200, offset: int = 0, genre: str | None = None,
                    artist_id: str | None = None) -> dict[str, Any]:
        """One page of the Library browser: ``artists`` / ``albums`` / ``songs`` /
        ``genres``, filtered by genre or artist, sorted, across all libraries.

        Each library contributes its first ``offset + limit`` items, already
        sorted by SQL; they're k-way merged with a key that mirrors that SQL
        exactly, so paging is right across peers. ``total`` is the true count
        (own + every peer's). Peers too old for the view endpoint contribute
        what they can and are listed in ``outdated_peers``."""
        raw_artist, artist_peer = split_id(artist_id) if artist_id else (None, None)
        own_ok = self.own is not None and artist_peer is None
        peers_ok = artist_id is None or artist_peer is not None
        # One library involved (the usual case — a client browsing its server's
        # library, or a server browsing its own): page it directly with SQL
        # OFFSET, exact and uncapped. Only several libraries need the merge,
        # where each contributes its first ``offset + limit`` rows (capped).
        lib_peers = self._library_peer_ids() if peers_ok else []
        if artist_peer is not None:
            lib_peers = [p for p in lib_peers if p == artist_peer]
        own_has = own_ok and bool(self.own.index.stats()["tracks"])
        single = (own_has and not lib_peers) or (not own_has and len(lib_peers) == 1)
        if single:
            src_offset, src_limit, skip = offset, limit, 0
        else:
            src_offset, src_limit, skip = 0, min(offset + limit, _MERGE_CAP), offset
        want = src_limit
        params = {"view": view, "order": order, "desc": int(desc), "limit": src_limit,
                  "offset": src_offset,
                  **({"genre": genre} if genre else {}), **({"artist": raw_artist} if raw_artist else {})}
        outdated: list[str] = []

        def reachable(total: int) -> int:
            # A merged view can only reach the first _MERGE_CAP rows; say so,
            # so clients stop asking instead of paging into empty pages.
            return total if single else min(total, _MERGE_CAP)

        def ask(peer: str) -> dict[str, Any]:
            try:
                return self._get(peer, "/api/library/own/view", params)
            except PeerNotFound:
                pass
            # An older peer: only its plain browse (albums/artists) is available.
            out: dict[str, Any] = {"_outdated": True}
            # (Its browse can't page: it only fills the first page.)
            if view in ("albums", "artists") and not genre and not raw_artist and not src_offset:
                try:
                    rows = self._get(peer, "/api/library/own/browse",
                                     {"order": order, "limit": min(500, want)}).get(view) or []
                except Exception as exc:  # noqa: BLE001 - contributes nothing
                    log.info("library federation: %s browse fallback failed: %s", peer, exc)
                    rows = []
                out[view], out["total"], out["_resort"] = rows, len(rows), True
            return out

        def peer_pages() -> list[tuple[str, dict[str, Any]]]:
            if not peers_ok:
                return []
            pages = []
            for peer, r in self._fan_out(ask, lib_peers):
                if artist_peer is not None and peer != artist_peer:
                    continue
                if r.get("_outdated"):
                    outdated.append(self._known().get(peer, peer))
                pages.append((peer, r))
            return pages

        def merged(own_rows: list[Any], pages: list[tuple[str, dict[str, Any]]], key_name: str,
                   convert: Callable[[str, Any], Any], key: Callable[[Any], Any]) -> list[Any]:
            lists = [own_rows]
            for peer, r in pages:
                rows = [convert(peer, x) for x in r.get(key_name) or []]
                if r.get("_resort"):
                    rows.sort(key=key)
                lists.append(rows)
            return list(heapq.merge(*lists, key=key))[skip:skip + limit]

        def done(page: dict[str, Any]) -> dict[str, Any]:
            return {**page, **({"outdated_peers": outdated} if outdated else {})}

        if view == "genres":
            gmerged: dict[str, dict[str, Any]] = {}
            rows = (self.own.list_genres() if self.own is not None else [])
            for peer, r in peer_pages():
                rows += [{**g, "artwork_url": self._art(peer, g.get("artwork_url"))}
                         for g in r.get("genres") or []]
            for g in rows:
                k = (g.get("name") or "").casefold()
                cur = gmerged.setdefault(k, {**g, "album_count": 0, "track_count": 0})
                cur["album_count"] += g.get("album_count") or 0
                cur["track_count"] += g.get("track_count") or 0
            items = sorted(gmerged.values(), key=lambda g: (g.get("name") or "").casefold())
            return done({"view": view, "genres": items[offset:offset + limit], "total": len(items)})
        if view == "artists":
            own_rows = [{**a, "service": "local"} for a in self.own.list_artists(
                limit=want, offset=src_offset, desc=desc, genre=genre)] if own_ok else []
            total = self.own.index.count_artists(genre=genre) if own_ok else 0
            pages = peer_pages()
            total += sum(int(r.get("total") or 0) for _p, r in pages)
            page = merged(own_rows, pages, "artists",
                          lambda peer, a: {**a, "id": fed_id(str(a["id"]), peer), "service": "local",
                                           "artwork_url": self._art(peer, a.get("artwork_url")),
                                           "instance": self._known().get(peer, peer)},
                          _artist_key(desc))
            return done({"view": view, "artists": page, "total": reachable(total)})
        if view == "albums":
            own_rows = self.own.list_albums(order=order, desc=desc, limit=want, offset=src_offset,
                                            genre=genre, artist_id=raw_artist) if own_ok else []
            total = self.own.index.count_albums(artist_id=raw_artist, genre=genre) if own_ok else 0
            pages = peer_pages()
            total += sum(int(r.get("total") or 0) for _p, r in pages)
            return done({"view": view, "total": reachable(total), "albums": merged(
                own_rows, pages, "albums", self._album, _album_key(order, desc))})
        if view == "songs":
            own_rows, total = [], 0
            if own_ok:
                own_rows, total = self.own.list_songs(order=order, desc=desc, limit=want,
                                                      offset=src_offset, genre=genre,
                                                      artist_id=raw_artist)
            pages = peer_pages()
            total += sum(int(r.get("total") or 0) for _p, r in pages)
            return done({"view": view, "total": reachable(total), "songs": merged(
                own_rows, pages, "songs", self._track, _song_key(order, desc))})
        raise ValueError(f"unknown library view {view!r}")

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
        q = r.get("quality") if isinstance(r.get("quality"), dict) else None
        return StreamSource(url=f"{_base(peer)}/stream/{r['token']}", mime_type=r.get("mime") or "",
                            headers={"X-Harmony-Key": key} if key else {},
                            label=r.get("label") or "Library", quality=q)

    def instance_name(self, track_id: str) -> str | None:
        """The peer's name for a federated id; None for this instance's own."""
        _raw, peer = split_id(track_id)
        return self._known().get(peer, peer) if peer else None

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


def _nc(text: Any) -> str:
    """SQLite ``COLLATE NOCASE``: folds ASCII letters only."""
    return "".join(c.lower() if c < "\x80" else c for c in (text or ""))


def _ordered(descending: bool) -> Callable[[Callable[[Any], tuple[Any, tuple]]], Callable[[Any], Any]]:
    """A merge key from ``item → (primary, ties)`` matching the library SQL:
    a ``None`` primary sinks to the end in either direction, only the primary
    flips with ``descending``, and ties always run ascending."""
    def wrap(fn: Callable[[Any], tuple[Any, tuple]]) -> Callable[[Any], Any]:
        def cmp(a: tuple[Any, tuple], b: tuple[Any, tuple]) -> int:
            (pa, ta), (pb, tb) = a, b
            if (pa is None) != (pb is None):
                return 1 if pa is None else -1
            if pa is not None and pa != pb:
                c = -1 if pa < pb else 1
                return -c if descending else c
            return (ta > tb) - (ta < tb)
        keyed = cmp_to_key(cmp)
        return lambda item: keyed(fn(item))
    return wrap


#: Sorts whose natural direction is high→low (mirrors library.py).
_ALBUM_DESC_FIRST = {"recent"}
_SONG_DESC_FIRST = {"recent", "quality"}


def _album_key(order: str, desc: bool) -> Callable[[Album], Any]:
    def parts(a: Album) -> tuple[Any, tuple]:
        title, artist, year = _nc(a.title), _nc(a.artist_name), a.year
        if order == "recent":
            return float((a.raw or {}).get("added_at") or 0), (title, a.id)
        if order == "title":
            return title, (artist, a.id)
        if order == "artist":
            return artist, (year is None, year or 0, title, a.id)
        return year, (title, a.id)  # "year"
    return _ordered(desc != (order in _ALBUM_DESC_FIRST))(parts)


def _artist_key(desc: bool) -> Callable[[dict[str, Any]], Any]:
    return _ordered(desc)(lambda a: (_nc(a.get("name")), (str(a.get("id")),)))


def _song_key(order: str, desc: bool) -> Callable[[Track], Any]:
    def parts(t: Track) -> tuple[Any, tuple]:
        s = (t.raw or {}).get("sort") or {}
        disc = s.get("disc") if s.get("disc") is not None else 1
        no = t.track_number if t.track_number is not None else 9999
        title, artist, album = _nc(t.title), _nc(t.artist_name), _nc(t.album)
        track_tie = (album, disc, no)
        end = (t.id,)
        if order == "title":
            return title, (artist, *end)
        if order == "album":
            return album, (disc, no, *end)
        if order == "year":
            return t.year, (*track_tie, *end)
        if order == "genre":
            return (_nc(t.genre) if t.genre else None), (artist, *track_tie, *end)
        if order == "duration":
            return t.duration_s, (title, *end)
        if order == "recent":
            return float(s.get("added_at") or 0), (*track_tie, *end)
        if order == "quality":
            q = t.quality or {}
            fallback = ((q.get("bit_depth") or 0) * 1000000 + (q.get("sample_rate") or 0) * 10
                        + (q.get("bitrate") or 0))
            return float(s["q"]) if s.get("q") is not None else float(fallback), (title, *end)
        return artist, (*track_tie, *end)  # "artist"
    return _ordered(desc != (order in _SONG_DESC_FIRST))(parts)


__all__ = ["FederatedLibraryProvider", "fed_id", "split_id"]
