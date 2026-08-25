from __future__ import annotations

import time

import pytest

from harmony.db import Database
from harmony.models import Service


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    yield database
    database.close()


# -- schema / migration -------------------------------------------------------


def test_migrate_is_idempotent_and_records_schema_version(tmp_path) -> None:
    path = tmp_path / "harmony.db"
    db1 = Database(path)
    db1.close()
    # Reopening must not blow up on "table already exists" or similar.
    db2 = Database(path)
    version = db2.cache_get("schema_version", max_age_s=10_000)
    assert version is not None
    db2.close()


# -- track_links -------------------------------------------------------


def test_put_link_round_trips(db: Database) -> None:
    db.put_link(Service.YTMUSIC, "yt1", Service.QOBUZ, "qo1", 0.93, "high")
    link = db.get_link(Service.YTMUSIC, "yt1", Service.QOBUZ)
    assert link is not None
    assert link["dst_id"] == "qo1"
    assert link["score"] == pytest.approx(0.93)
    assert link["confidence"] == "high"


def test_put_link_is_symmetric(db: Database) -> None:
    db.put_link(Service.YTMUSIC, "yt1", Service.QOBUZ, "qo1", 0.93, "high")
    reverse = db.get_link(Service.QOBUZ, "qo1", Service.YTMUSIC)
    assert reverse is not None
    assert reverse["dst_id"] == "yt1"


def test_get_link_missing_returns_none(db: Database) -> None:
    assert db.get_link(Service.YTMUSIC, "nope", Service.QOBUZ) is None


def test_put_link_overwrites_existing(db: Database) -> None:
    db.put_link(Service.YTMUSIC, "yt1", Service.QOBUZ, "qo1", 0.80, "low")
    db.put_link(Service.YTMUSIC, "yt1", Service.QOBUZ, "qo2", 0.95, "high")
    link = db.get_link(Service.YTMUSIC, "yt1", Service.QOBUZ)
    assert link is not None
    assert link["dst_id"] == "qo2"
    assert link["confidence"] == "high"


def test_forget_link_removes_both_directions(db: Database) -> None:
    db.put_link(Service.YTMUSIC, "yt1", Service.QOBUZ, "qo1", 0.93, "high")
    db.forget_link(Service.YTMUSIC, "yt1", Service.QOBUZ)
    assert db.get_link(Service.YTMUSIC, "yt1", Service.QOBUZ) is None
    assert db.get_link(Service.QOBUZ, "qo1", Service.YTMUSIC) is None


# -- playlist_links -------------------------------------------------------


def test_link_playlists_round_trip(db: Database) -> None:
    db.link_playlists("local1", "My Playlist", ytmusic_id="yt-pl1", qobuz_id="qo-pl1")
    link = db.get_playlist_link("local1")
    assert link is not None
    assert link["ytmusic_id"] == "yt-pl1"
    assert link["qobuz_id"] == "qo-pl1"
    assert link in db.list_playlist_links()


def test_link_playlists_partial_update_preserves_other_side(db: Database) -> None:
    db.link_playlists("local1", "My Playlist", ytmusic_id="yt-pl1")
    db.link_playlists("local1", "My Playlist", qobuz_id="qo-pl1")
    link = db.get_playlist_link("local1")
    assert link is not None
    assert link["ytmusic_id"] == "yt-pl1"
    assert link["qobuz_id"] == "qo-pl1"


def test_unlink_playlists(db: Database) -> None:
    db.link_playlists("local1", "My Playlist", ytmusic_id="yt-pl1")
    db.unlink_playlists("local1")
    assert db.get_playlist_link("local1") is None


# -- snapshots -------------------------------------------------------


def test_save_and_load_snapshot(db: Database) -> None:
    payload = {"tracks": [{"id": "t1", "title": "Song"}]}
    snap_id = db.save_snapshot(Service.YTMUSIC, "pl1", payload)
    loaded = db.load_snapshot(snap_id)
    assert loaded == payload


def test_list_snapshots_orders_newest_first(db: Database) -> None:
    first = db.save_snapshot(Service.YTMUSIC, "pl1", {"n": 1})
    time.sleep(0.01)
    second = db.save_snapshot(Service.YTMUSIC, "pl1", {"n": 2})
    rows = db.list_snapshots(Service.YTMUSIC, "pl1")
    assert [r["id"] for r in rows] == [second, first]


def test_load_missing_snapshot_returns_none(db: Database) -> None:
    assert db.load_snapshot(99999) is None


# -- kv / cache -------------------------------------------------------


def test_cache_put_get_round_trip(db: Database) -> None:
    db.cache_put("search:foo", {"tracks": [1, 2, 3]})
    assert db.cache_get("search:foo", max_age_s=1000) == {"tracks": [1, 2, 3]}


def test_cache_get_missing_key_returns_none(db: Database) -> None:
    assert db.cache_get("nope", max_age_s=1000) is None


def test_cache_get_expires(db: Database) -> None:
    db.cache_put("stale", {"v": 1})
    # updated_at is "now", so an already-elapsed window of -1s is instantly expired.
    assert db.cache_get("stale", max_age_s=-1) is None


def test_cache_put_overwrites(db: Database) -> None:
    db.cache_put("k", {"v": 1})
    db.cache_put("k", {"v": 2})
    assert db.cache_get("k", max_age_s=1000) == {"v": 2}
