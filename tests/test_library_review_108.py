"""1.0.8 review fixes: true totals, SQL-side album/artist sorting, exact federated
merges, post-upgrade rescan, outdated peers and the own instance name."""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from harmony.library import ArtSigner, LibraryIndex
from harmony.library_federation import FederatedLibraryProvider, PeerNotFound
from harmony.providers.local import LocalLibraryProvider
from harmony.web.api import Engine, album_to_dict, track_to_dict

ALBUM_ORDERS = ("year", "recent", "title", "artist")
SONG_ORDERS = ("title", "artist", "album", "year", "genre", "duration", "recent", "quality")


def _fill(idx: LibraryIndex, tag: str, albums: int, seed: int) -> None:
    rnd = random.Random(seed)
    for i in range(albums):
        artist = f"{rnd.choice('AbCdÉe')}rtist {rnd.randrange(12)}"
        album = f"{rnd.choice('xYzÀw')}lbum {rnd.randrange(40)} {tag}{i}"
        year = rnd.choice([None, 1990, 1990, 2001, 2010])
        genre = rnd.choice([None, "Rock", "jazz", "Jazz"])
        added = float(rnd.randrange(50))  # many ties on purpose
        for n in range(1, 3):
            tid = f"{tag}-{i}-{n}"
            idx._conn.execute(
                "INSERT INTO tracks (id, path, size, mtime, title, artist, artist_id, album, "
                "album_artist, album_artist_id, album_id, track_no, disc_no, year, duration_s, "
                "mime, genre, bit_depth, sample_rate, bitrate, haystack, added_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, f"/m/{tid}.flac", 1, 1.0, f"Song {rnd.randrange(30)} {tag}{i}-{n}", artist,
                 f"ar-{artist}", album, artist, f"ar-{artist}", f"al-{tag}{i}", n,
                 rnd.choice([None, 1, 2]), year, rnd.choice([None, 100, 200]), "audio/flac",
                 genre, rnd.choice([None, 16, 24]), rnd.choice([None, 44100, 96000]),
                 rnd.choice([None, 900000]), "x", added))
    idx._conn.commit()


@pytest.fixture
def idx(tmp_path):
    i = LibraryIndex(tmp_path / "a.db")
    _fill(i, "o", 320, 1)
    yield i
    i.close()


def _pages(fetch, total_hint=None, size=50):
    seen, off = [], 0
    while True:
        rows = fetch(size, off)
        if not rows:
            return seen
        seen += rows
        off += size


# -- 2. album desc in SQL ---------------------------------------------------------


@pytest.mark.parametrize("order", ALBUM_ORDERS)
@pytest.mark.parametrize("desc", [False, True])
def test_album_pages_never_overlap_for_every_order_and_direction(idx, order, desc):
    ids = _pages(lambda n, o: [a["album_id"] for a in idx.albums(order=order, desc=desc, limit=n, offset=o)])
    assert len(ids) == len(set(ids)) == idx.count_albums() == 320


def test_album_desc_reverses_the_sort_not_just_the_page(idx):
    asc = [a["album_id"] for a in idx.albums(order="title", limit=1000)]
    page2_desc = [a["album_id"] for a in idx.albums(order="title", desc=True, limit=50, offset=50)]
    assert page2_desc and set(page2_desc).isdisjoint(a["album_id"] for a in idx.albums(
        order="title", desc=True, limit=50))
    assert page2_desc == list(reversed(asc))[50:100]
    newest = idx.albums(order="recent", limit=1)[0]["added_at"]
    assert newest == max(a["added_at"] for a in idx.albums(limit=1000))  # recent = newest first


def test_artists_page_and_desc(idx):
    names = _pages(lambda n, o: [a["id"] for a in idx.artists(limit=n, offset=o)], size=7)
    assert len(names) == len(set(names)) == idx.count_artists()
    assert [a["id"] for a in idx.artists(desc=True)] == list(reversed(names))


# -- federation harness: a peer that is a second library --------------------------


@pytest.fixture
def fed(tmp_path, idx):
    peer_idx = LibraryIndex(tmp_path / "b.db")
    _fill(peer_idx, "p", 130, 2)
    union = LibraryIndex(tmp_path / "u.db")
    for src in (idx, peer_idx):
        for r in src._conn.execute("SELECT * FROM tracks").fetchall():
            union._conn.execute(f"INSERT INTO tracks ({', '.join(r.keys())}) VALUES "
                                f"({', '.join('?' for _ in r.keys())})", tuple(r))
    union._conn.commit()
    own = LocalLibraryProvider(idx, ArtSigner(b"k"))
    peer = LocalLibraryProvider(peer_idx, ArtSigner(b"k"))

    def serve(path, params=None, timeout=0):
        assert path == "/api/library/own/view"
        v, kw = params["view"], dict(order=params["order"], desc=bool(params["desc"]),
                                     limit=params["limit"], offset=params["offset"])
        if v == "albums":
            return {"albums": [album_to_dict(a) for a in peer.list_albums(**kw)],
                    "total": peer_idx.count_albums()}
        if v == "artists":
            return {"artists": peer.list_artists(limit=kw["limit"], desc=kw["desc"]),
                    "total": peer_idx.count_artists()}
        rows, total = peer.list_songs(**kw)
        return {"songs": [track_to_dict(t) for t in rows], "total": total}

    prov = FederatedLibraryProvider(own, lambda: [{"host": "h", "port": 1, "name": "Peer"}], lambda: None)
    prov._get = lambda peer_id, path, params=None, timeout=0: serve(path, params)
    prov._library_peer_ids = lambda: ["h:1"]
    prov._fan_out = lambda fn, peers=None: [(p, fn(p)) for p in (["h:1"] if peers is None else peers)]
    yield SimpleNamespace(prov=prov, union=LocalLibraryProvider(union, ArtSigner(b"k")),
                          own=idx, peer_idx=peer_idx)
    peer_idx.close()
    union.close()


def _strip(i: str) -> str:
    return i.partition("@")[0]


# -- 1 + 3 + 4: totals, recent, exact merge ---------------------------------------


@pytest.mark.parametrize("order", ALBUM_ORDERS)
@pytest.mark.parametrize("desc", [False, True])
def test_federated_albums_total_and_pages_match_one_big_library(fed, order, desc):
    first = fed.prov.browse_view("albums", order=order, desc=desc, limit=60, offset=0)
    assert first["total"] == 450  # the real count, not len(rows)
    got = _pages(lambda n, o: [_strip(a.id) for a in fed.prov.browse_view(
        "albums", order=order, desc=desc, limit=n, offset=o)["albums"]], size=60)
    assert len(got) == len(set(got)) == 450
    want = [a["album_id"] for a in fed.union.index.albums(order=order, desc=desc, limit=1000)]
    if order in ("title", "year", "artist", "recent"):
        # Same sequence unless a full tie differs only by id; compare as sort keys.
        key = {"title": "album", "year": "year", "artist": "album_artist", "recent": "added_at"}[order]
        look = {a["album_id"]: a[key] for a in fed.union.index.albums(limit=1000)}

        def norm(v):
            return v.lower() if isinstance(v, str) else v
        assert [norm(look[i]) for i in got] == [norm(look[i]) for i in want]


def test_federated_recent_albums_carry_added_at(fed):
    d = album_to_dict(fed.prov.own.list_albums(order="recent", limit=1)[0])
    assert d["added_at"] is not None
    peer_album = fed.prov.browse_view("albums", order="recent", limit=1000)["albums"]
    assert all((a.raw or {}).get("added_at") is not None for a in peer_album)
    stamps = [a.raw["added_at"] for a in peer_album]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.parametrize("order", SONG_ORDERS)
@pytest.mark.parametrize("desc", [False, True])
def test_federated_song_pages_are_disjoint_and_complete(fed, order, desc):
    first = fed.prov.browse_view("songs", order=order, desc=desc, limit=100)
    assert first["total"] == 900
    got = _pages(lambda n, o: [_strip(t.id) for t in fed.prov.browse_view(
        "songs", order=order, desc=desc, limit=n, offset=o)["songs"]], size=100)
    assert len(got) == len(set(got)) == 900
    want = [_strip(t.id) for t in fed.union.list_songs(order=order, desc=desc, limit=2000)[0]]
    assert got == want


@pytest.mark.parametrize("desc", [False, True])
def test_federated_artists_true_total_and_paging(fed, desc):
    total = fed.prov.browse_view("artists", desc=desc, limit=5)["total"]
    assert total == fed.own.count_artists() + fed.peer_idx.count_artists()
    names = _pages(lambda n, o: [(_strip(a["id"]), a["name"]) for a in fed.prov.browse_view(
        "artists", desc=desc, limit=n, offset=o)["artists"]], size=5)
    assert len(names) == total  # no 2000 cap, every row reachable


# -- 5. rescan after upgrade ---------------------------------------------------------


def _v107_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tracks (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, size INTEGER, "
                "mtime REAL, title TEXT, artist TEXT, artist_id TEXT, album TEXT, album_artist TEXT, "
                "album_artist_id TEXT, album_id TEXT, track_no INTEGER, disc_no INTEGER, year INTEGER, "
                "date TEXT, duration_s INTEGER, isrc TEXT, mb_releasegroup TEXT, mb_release TEXT, "
                "mb_artist TEXT, mime TEXT NOT NULL, haystack TEXT NOT NULL, added_at REAL NOT NULL)")
    con.execute("INSERT INTO tracks VALUES ('x','/m/a.wav',1,123.0,'T','A','a','Al','A','a','al',"
                "1,1,2000,'2000',1,NULL,NULL,NULL,NULL,'audio/wav','t',0)")
    con.commit()
    con.close()


def test_migration_sets_flag_and_engine_rescans_once(tmp_path):
    db = tmp_path / "old.db"
    _v107_db(db)
    idx = LibraryIndex(db)
    assert idx.migrated is True
    calls = []
    fake = SimpleNamespace(library_scan=lambda **kw: calls.append(kw) or {"ok": True})
    Engine._rescan_after_migration(fake, idx)
    assert calls == [{"wait": True}] and idx.migrated is False
    idx.close()
    assert LibraryIndex(db).migrated is False  # already current: nothing to redo
    fresh = LibraryIndex(tmp_path / "fresh.db")
    assert fresh.migrated is False
    fresh.close()


def test_failed_rescan_keeps_the_flag(tmp_path):
    _v107_db(tmp_path / "old.db")
    idx = LibraryIndex(tmp_path / "old.db")
    fake = SimpleNamespace(library_scan=lambda **kw: {"ok": False})
    Engine._rescan_after_migration(fake, idx)
    assert idx.migrated is True
    idx.close()


# -- 6. outdated peers ------------------------------------------------------------------


def test_old_peer_falls_back_to_browse_and_is_reported(fed):
    prov = fed.prov
    peer_albums = [album_to_dict(a) for a in LocalLibraryProvider(
        fed.peer_idx, ArtSigner(b"k")).list_albums(limit=500)]

    def get(peer, path, params=None, timeout=0):
        if path == "/api/library/own/view":
            raise PeerNotFound("404")
        return {"albums": peer_albums, "artists": [{"id": "z", "name": "Zed", "album_count": 1}]}

    prov._get = get
    page = prov.browse_view("albums", order="title", limit=1000)
    assert page["outdated_peers"] == ["Peer"]
    assert page["total"] == 320 + len(peer_albums)
    assert len(page["albums"]) == page["total"]
    arts = prov.browse_view("artists", limit=2000)
    assert arts["outdated_peers"] == ["Peer"] and any(a["name"] == "Zed" for a in arts["artists"])
    songs = prov.browse_view("songs", limit=10)
    assert songs["outdated_peers"] == ["Peer"] and songs["total"] == 640


# -- 15. own instance name ----------------------------------------------------------------


def test_stream_info_prefers_the_mesh_instance_name():
    prov = SimpleNamespace(instance_name=lambda tid: None)
    fake = SimpleNamespace(_library_provider=lambda: prov, _mesh=SimpleNamespace(_name="harmony-den-8080"))
    out = Engine._stream_info(fake, "local", "t1", SimpleNamespace(quality=None))
    assert out["source"]["instance"] == "harmony-den-8080"
    fake._mesh = None
    import socket
    assert Engine._stream_info(fake, "local", "t1", SimpleNamespace(quality=None))["source"]["instance"] \
        == socket.gethostname()


def test_interrupted_upgrade_rescan_is_redone_next_launch(tmp_path):
    _v107_db(tmp_path / "old.db")
    LibraryIndex(tmp_path / "old.db").close()        # upgraded, then "crashed" before rescanning
    assert LibraryIndex(tmp_path / "old.db").migrated is True


def test_rescan_reporting_an_error_keeps_the_flag(tmp_path):
    _v107_db(tmp_path / "old.db")
    idx = LibraryIndex(tmp_path / "old.db")
    fake = SimpleNamespace(library_scan=lambda **kw: {"ok": True, "scan": {"error": "disk gone"}})
    Engine._rescan_after_migration(fake, idx)
    assert idx.migrated is True
    idx.close()


def test_a_single_peer_library_pages_directly_with_no_cap():
    """A desktop browsing its server's library (no library of its own) must reach
    every row: the page is asked of the peer with the real offset, uncapped."""
    from harmony.library_federation import FederatedLibraryProvider

    seen = []

    def get(peer, path, params=None, timeout=0):
        seen.append(dict(params or {}))
        return {"songs": [], "total": 30_000}

    prov = FederatedLibraryProvider(None, lambda: [{"host": "srv", "port": 8080, "name": "srv"}],
                                    lambda: None)
    prov._library_peer_ids = lambda: ["srv:8080"]
    prov._get = get
    page = prov.browse_view("songs", order="title", limit=200, offset=12_000)
    assert seen[-1]["offset"] == 12_000 and seen[-1]["limit"] == 200
    assert page["total"] == 30_000


def test_a_merged_view_reports_only_what_it_can_reach():
    from harmony.library_federation import _MERGE_CAP, FederatedLibraryProvider

    def get(peer, path, params=None, timeout=0):
        assert params["offset"] == 0 and params["limit"] <= _MERGE_CAP
        return {"songs": [], "total": 20_000}

    prov = FederatedLibraryProvider(None, lambda: [], lambda: None)
    prov._library_peer_ids = lambda: ["a:1", "b:1"]
    prov._get = get
    page = prov.browse_view("songs", order="title", limit=200, offset=9_000)
    assert page["total"] == _MERGE_CAP and page["songs"] == []
