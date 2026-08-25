from __future__ import annotations

import pytest
from conftest import FakeProvider

from harmony.db import Database
from harmony.models import Playlist, Service, Track
from harmony.sync import SyncDirection, SyncEngine


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    yield database
    database.close()


def _track(**kwargs) -> Track:
    return Track(**kwargs)


# -- MIRROR -------------------------------------------------------


def test_mirror_no_action_when_already_present_and_removes_orphan(db: Database) -> None:
    yt_common = _track(id="yt1", title="Song One", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
    yt_unmatched = _track(
        id="yt2", title="Totally Different Name", service=Service.YTMUSIC, artists=["Other"], duration_s=999
    )
    qo_common = _track(id="qo1", title="Song One", service=Service.QOBUZ, artists=["Artist"], duration_s=200)
    qo_orphan = _track(id="qo-extra", title="Extra Song", service=Service.QOBUZ, artists=["Someone"], duration_s=100)

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[qo_common])
    provider_yt.playlists["yt-pl"] = [yt_common, yt_unmatched]
    provider_qo.playlists["qo-pl"] = [qo_common, qo_orphan]

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)

    kinds = sorted(act.kind for act in plan.actions)
    assert kinds == ["remove", "unmatched"]
    remove_action = next(act for act in plan.actions if act.kind == "remove")
    assert remove_action.track.id == "qo-extra"
    unmatched_action = next(act for act in plan.actions if act.kind == "unmatched")
    assert unmatched_action.track.id == "yt2"


def test_mirror_apply_removes_orphan_and_snapshots(db: Database) -> None:
    yt_common = _track(id="yt1", title="Song One", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
    qo_common = _track(id="qo1", title="Song One", service=Service.QOBUZ, artists=["Artist"], duration_s=200)
    qo_orphan = _track(id="qo-extra", title="Extra Song", service=Service.QOBUZ, artists=["Someone"], duration_s=100)

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[qo_common])
    provider_yt.playlists["yt-pl"] = [yt_common]
    provider_qo.playlists["qo-pl"] = [qo_common, qo_orphan]

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    report = engine.apply(plan, snapshot_before_sync=True)

    remaining_ids = {t.id for t in provider_qo.playlists["qo-pl"]}
    assert remaining_ids == {"qo1"}
    assert [t.id for t in report.removed] == ["qo-extra"]
    assert report.failed == []

    assert db.list_snapshots(Service.YTMUSIC, "yt-pl")
    assert db.list_snapshots(Service.QOBUZ, "qo-pl")


# -- TWO_WAY -------------------------------------------------------


def test_two_way_unions_both_sides_and_never_removes(db: Database) -> None:
    common_yt = _track(id="yt-common", title="Common Song", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
    common_qo = _track(id="qo-common", title="Common Song", service=Service.QOBUZ, artists=["Artist"], duration_s=200)
    only_a_yt = _track(id="yt-only", title="Only A Song", service=Service.YTMUSIC, artists=["Artist"], duration_s=150)
    only_a_on_qo = _track(id="qo-only-match", title="Only A Song", service=Service.QOBUZ, artists=["Artist"], duration_s=150)
    only_b_qo = _track(id="qo-only", title="Only B Song", service=Service.QOBUZ, artists=["Artist"], duration_s=170)
    only_b_on_yt = _track(id="yt-only-match", title="Only B Song", service=Service.YTMUSIC, artists=["Artist"], duration_s=170)

    provider_yt = FakeProvider(Service.YTMUSIC, catalog=[common_yt, only_b_on_yt])
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[common_qo, only_a_on_qo])
    provider_yt.playlists["yt-pl"] = [common_yt, only_a_yt]
    provider_qo.playlists["qo-pl"] = [common_qo, only_b_qo]

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.TWO_WAY)
    assert all(act.kind != "remove" for act in plan.actions)
    add_targets = {(act.target, act.track.id) for act in plan.actions if act.kind == "add"}
    assert add_targets == {(Service.QOBUZ, "qo-only-match"), (Service.YTMUSIC, "yt-only-match")}

    report = engine.apply(plan, snapshot_before_sync=False)
    assert report.failed == []
    assert {t.id for t in provider_yt.playlists["yt-pl"]} == {"yt-common", "yt-only", "yt-only-match"}
    assert {t.id for t in provider_qo.playlists["qo-pl"]} == {"qo-common", "qo-only", "qo-only-match"}


# -- failure isolation -------------------------------------------------------


def test_apply_isolates_a_single_failed_add(db: Database) -> None:
    yt1 = _track(id="yt1", title="Song One", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
    yt2 = _track(id="yt2", title="Song Two", service=Service.YTMUSIC, artists=["Artist"], duration_s=210)
    qo1 = _track(id="qo1", title="Song One", service=Service.QOBUZ, artists=["Artist"], duration_s=200)
    qo2 = _track(id="qo2", title="Song Two", service=Service.QOBUZ, artists=["Artist"], duration_s=210)

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[qo1, qo2])
    provider_qo.fail_ids = {"qo2"}
    provider_yt.playlists["yt-pl"] = [yt1, yt2]
    provider_qo.playlists["qo-pl"] = []

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    report = engine.apply(plan, snapshot_before_sync=False)

    assert [t.id for t in report.added] == ["qo1"]
    assert len(report.failed) == 1
    failed_action, message = report.failed[0]
    assert failed_action.track.id == "qo2"
    assert "qo2" in message
    assert {t.id for t in provider_qo.playlists["qo-pl"]} == {"qo1"}


# -- clone_playlist -------------------------------------------------------


def test_clone_playlist_creates_and_plans_fill(db: Database) -> None:
    yt1 = _track(id="yt1", title="Song One", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
    qo1 = _track(id="qo1", title="Song One", service=Service.QOBUZ, artists=["Artist"], duration_s=200)

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[qo1])
    provider_yt.playlists["yt-pl"] = [yt1]

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    src = Playlist(id="yt-pl", title="My Mix", service=Service.YTMUSIC)

    plan = engine.clone_playlist(src, Service.QOBUZ)

    assert plan.target.title == "My Mix"
    assert plan.target.service == Service.QOBUZ
    assert plan.target.id in provider_qo.playlists
    assert any(act.kind == "add" and act.track.id == "qo1" for act in plan.actions)
