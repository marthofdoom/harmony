from __future__ import annotations

import pytest
from conftest import FakeProvider

from harmony.db import Database
from harmony.models import Playlist, Service, Track
from harmony.sync import SyncAction, SyncDirection, SyncEngine, SyncPlan


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    yield database
    database.close()


def _track(**kwargs) -> Track:
    return Track(**kwargs)


class _CountingProvider(FakeProvider):
    """FakeProvider that records every add_tracks/remove_tracks call it gets,
    so tests can assert on how many provider round-trips a sync issued."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.add_calls: list[list[str]] = []
        self.remove_calls: list[list[str]] = []

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        self.add_calls.append(list(track_ids))
        super().add_tracks(playlist_id, track_ids)

    def remove_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        self.remove_calls.append(list(track_ids))
        super().remove_tracks(playlist_id, track_ids)


# -- MIRROR -------------------------------------------------------


def test_mirror_no_action_when_already_present_and_suppresses_removal_when_unmatched(db: Database) -> None:
    """Regression for bug 1's original repro shape: a genuinely-unmatched
    source track used to also trigger deletion of an unrelated orphan on the
    target. Since we can no longer trust ANY removal while a source track's
    match outcome is undetermined, the orphan must survive and the plan must
    say why."""
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
    assert kinds == ["unmatched"]
    unmatched_action = next(act for act in plan.actions if act.kind == "unmatched")
    assert unmatched_action.track.id == "yt2"
    assert plan.notes, "plan should explain why removals were withheld"
    assert "unmatched" in plan.summary()

    report = engine.apply(plan, snapshot_before_sync=False)
    assert report.removed == []
    assert {t.id for t in provider_qo.playlists["qo-pl"]} == {"qo1", "qo-extra"}


def test_mirror_never_deletes_a_target_track_whose_source_match_merely_failed(db: Database) -> None:
    """Bug 1's exact shape: a target track (q9) that genuinely has a source
    counterpart (v9) but which the matcher could not find (v9's search pool
    doesn't even contain q9) must never be deleted."""
    v9 = _track(id="v9", title="Weird Title Nobody Matches", service=Service.YTMUSIC, artists=["Obscure"], duration_s=321)
    q9 = _track(id="q9", title="Weird Title Nobody Matches", service=Service.QOBUZ, artists=["Obscure"], duration_s=321)
    decoy = _track(id="qo-decoy", title="Completely Unrelated Song", service=Service.QOBUZ, artists=["Nobody"], duration_s=10)

    provider_yt = FakeProvider(Service.YTMUSIC)
    # q9 is deliberately absent from the searchable catalog, simulating a
    # real-world search miss even though it is present in the playlist.
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[decoy])
    provider_yt.playlists["yt-pl"] = [v9]
    provider_qo.playlists["qo-pl"] = [q9]

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    assert [act.kind for act in plan.actions] == ["unmatched"]

    report = engine.apply(plan, snapshot_before_sync=False)
    assert report.removed == []
    assert {t.id for t in provider_qo.playlists["qo-pl"]} == {"q9"}


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


# -- normalise() / stale remove after UI resolution ------------------------


def test_normalise_drops_remove_that_matches_a_resolved_add(db: Database) -> None:
    """Direct unit test of SyncPlan.normalise(): a remove and an add that
    target the same (service, playlist, track id) must collapse to just the
    add, regardless of who mutated the plan's actions."""
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)
    resolved = _track(id="q9", title="Resolved", service=Service.QOBUZ)
    other = _track(id="qo-keep-remove", title="Still Gone", service=Service.QOBUZ)

    plan = SyncPlan(
        source=a,
        target=b,
        actions=[
            SyncAction(kind="remove", target=Service.QOBUZ, track=resolved, match=None, target_playlist_id="qo-pl"),
            SyncAction(kind="remove", target=Service.QOBUZ, track=other, match=None, target_playlist_id="qo-pl"),
            SyncAction(kind="add", target=Service.QOBUZ, track=resolved, match=None, target_playlist_id="qo-pl"),
        ],
    )

    plan.normalise()

    kinds_by_id = {(act.kind, act.track.id) for act in plan.actions}
    assert ("remove", "q9") not in kinds_by_id
    assert ("add", "q9") in kinds_by_id
    assert ("remove", "qo-keep-remove") in kinds_by_id


def test_apply_never_deletes_a_track_the_ui_just_resolved_to_add(db: Database) -> None:
    """Integration shape of bug 2: the plan holds a stale ``remove`` for a
    target track that an "unmatched" action was just resolved (by the UI, in
    place) to add. apply() must not run ADD then DELETE on the same track."""
    q9 = _track(id="q9", title="Real Counterpart", service=Service.QOBUZ, artists=["Artist"], duration_s=200)

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[q9])
    provider_yt.playlists["yt-pl"] = []
    provider_qo.playlists["qo-pl"] = [q9]

    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    # Hand-build a plan the way a real one could look right before apply:
    # an action that used to be "unmatched" got flipped to "add" targeting
    # q9 (mirroring ui/sync_page.py's _use_candidate), while a leftover
    # "remove" for that same q9 is still sitting in the action list.
    resolved_action = SyncAction(kind="add", target=Service.QOBUZ, track=q9, match=None, target_playlist_id="qo-pl")
    stale_remove = SyncAction(kind="remove", target=Service.QOBUZ, track=q9, match=None, target_playlist_id="qo-pl")
    plan = SyncPlan(source=a, target=b, actions=[resolved_action, stale_remove])

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    report = engine.apply(plan, snapshot_before_sync=False)

    assert report.removed == []
    assert {t.id for t in provider_qo.playlists["qo-pl"]} == {"q9"}


# -- manual-resolution link caching -----------------------------------------


def test_apply_records_manual_link_for_user_resolved_pick_not_top_candidate(db: Database) -> None:
    """Bug 3: when the user resolves an unmatched row to a candidate other
    than the top-ranked one, the cached link must point at the track the
    user actually picked, with confidence "manual" and score 1.0 — not the
    top candidate's unrelated score glued onto the user's chosen id."""
    source = _track(id="v9", title="Ambiguous Song", service=Service.YTMUSIC, artists=["Some Artist"], duration_s=200)
    cand_a = _track(id="qo-a", title="Somewhat Similar A", service=Service.QOBUZ, artists=["Other Artist"], duration_s=205)
    cand_b = _track(id="qo-b", title="Somewhat Similar B", service=Service.QOBUZ, artists=["Other Artist"], duration_s=205)

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[cand_a, cand_b])
    provider_yt.playlists["yt-pl"] = [source]
    provider_qo.playlists["qo-pl"] = []

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    unmatched = next(act for act in plan.actions if act.kind == "unmatched")
    assert unmatched.match.confidence not in ("exact", "high")  # sanity: genuinely unmatched

    best_id = unmatched.match.best.track.id
    other_candidate = next(c for c in unmatched.match.candidates if c.track.id != best_id)

    # Simulate the UI resolving to the candidate that is NOT top-ranked.
    unmatched.kind = "add"
    unmatched.track = other_candidate.track

    report = engine.apply(plan, snapshot_before_sync=False)
    assert report.failed == []

    link = db.get_link(Service.YTMUSIC, "v9", Service.QOBUZ)
    assert link is not None
    assert link["dst_id"] == other_candidate.track.id
    assert link["confidence"] == "manual"
    assert link["score"] == 1.0


def test_apply_never_caches_a_link_with_confidence_none(db: Database) -> None:
    """Even when the user confirms the (only, poor) candidate as-is — so the
    applied track id equals match.best's id — a link must never be cached
    with confidence "none": matching._match_one would treat it as gospel and
    rule 1's removal guard would treat it as permanently unmatched."""
    source = _track(id="v9", title="Totally Unique Title", service=Service.YTMUSIC, artists=["Artist X"], duration_s=400)
    only_cand = _track(id="qo-only", title="Completely Different", service=Service.QOBUZ, artists=["Nobody"], duration_s=1)

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[only_cand])
    provider_yt.playlists["yt-pl"] = [source]
    provider_qo.playlists["qo-pl"] = []

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    unmatched = next(act for act in plan.actions if act.kind == "unmatched")
    assert unmatched.match.confidence == "none"
    assert unmatched.match.best.track.id == only_cand.id

    # User confirms the same (poor) candidate that was already ranked best.
    unmatched.kind = "add"
    unmatched.track = only_cand

    report = engine.apply(plan, snapshot_before_sync=False)
    assert report.failed == []

    link = db.get_link(Service.YTMUSIC, "v9", Service.QOBUZ)
    assert link is not None
    assert link["confidence"] != "none"
    assert link["confidence"] == "manual"
    assert link["score"] == 1.0


# -- batched provider calls ---------------------------------------------------


def test_apply_batches_adds_and_removes_into_one_provider_call_per_group(db: Database) -> None:
    """Bug 4: apply() must group same (service, playlist, kind) actions into
    a single add_tracks/remove_tracks call rather than one call per track."""
    src_tracks = [
        _track(id=f"yt{i}", title=f"Song {i}", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
        for i in range(3)
    ]
    dst_tracks = [
        _track(id=f"qo{i}", title=f"Song {i}", service=Service.QOBUZ, artists=["Artist"], duration_s=200)
        for i in range(3)
    ]
    orphan = _track(id="qo-orphan", title="Orphan", service=Service.QOBUZ, artists=["Artist"], duration_s=200)

    provider_yt = _CountingProvider(Service.YTMUSIC)
    provider_qo = _CountingProvider(Service.QOBUZ, catalog=dst_tracks)
    provider_yt.playlists["yt-pl"] = src_tracks
    provider_qo.playlists["qo-pl"] = [orphan]

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    assert sorted(act.kind for act in plan.actions) == ["add", "add", "add", "remove"]

    report = engine.apply(plan, snapshot_before_sync=False)
    assert report.failed == []

    assert len(provider_qo.add_calls) == 1
    assert set(provider_qo.add_calls[0]) == {"qo0", "qo1", "qo2"}
    assert len(provider_qo.remove_calls) == 1
    assert provider_qo.remove_calls[0] == ["qo-orphan"]


def test_apply_batch_failure_falls_back_to_per_track_isolation(db: Database) -> None:
    """When the single batched call raises, apply() must retry the batch's
    actions individually rather than failing the whole group."""
    yt1 = _track(id="yt1", title="Song One", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
    yt2 = _track(id="yt2", title="Song Two", service=Service.YTMUSIC, artists=["Artist"], duration_s=210)
    qo1 = _track(id="qo1", title="Song One", service=Service.QOBUZ, artists=["Artist"], duration_s=200)
    qo2 = _track(id="qo2", title="Song Two", service=Service.QOBUZ, artists=["Artist"], duration_s=210)

    provider_yt = _CountingProvider(Service.YTMUSIC)
    provider_qo = _CountingProvider(Service.QOBUZ, catalog=[qo1, qo2])
    provider_qo.fail_ids = {"qo2"}
    provider_yt.playlists["yt-pl"] = [yt1, yt2]
    provider_qo.playlists["qo-pl"] = []

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    report = engine.apply(plan, snapshot_before_sync=False)

    # One batched attempt for both, then a per-track retry of each (2 more
    # single-id calls) once the batch raises.
    assert len(provider_qo.add_calls) == 3
    assert sorted(provider_qo.add_calls[0]) == ["qo1", "qo2"]
    assert provider_qo.add_calls[1:] == [["qo1"], ["qo2"]]
    assert [t.id for t in report.added] == ["qo1"]
    assert len(report.failed) == 1
    assert report.failed[0][0].track.id == "qo2"
    assert {t.id for t in provider_qo.playlists["qo-pl"]} == {"qo1"}


# -- same-service TWO_WAY playlist targeting ---------------------------------


def test_two_way_same_service_targets_correct_playlist_each_way(db: Database) -> None:
    """'Also fix': keying applied actions by Service alone collapses two
    distinct playlists on the same service into one. A TWO_WAY sync between
    two YouTube Music playlists must add to each playlist independently."""
    a1 = _track(id="a1", title="A Song", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
    b1 = _track(id="b1", title="B Song", service=Service.YTMUSIC, artists=["Artist"], duration_s=210)

    provider_yt = FakeProvider(Service.YTMUSIC, catalog=[a1, b1])
    provider_yt.playlists["pa"] = [a1]
    provider_yt.playlists["pb"] = [b1]

    engine = SyncEngine({Service.YTMUSIC: provider_yt}, db)
    pa = Playlist(id="pa", title="Playlist A", service=Service.YTMUSIC)
    pb = Playlist(id="pb", title="Playlist B", service=Service.YTMUSIC)

    plan = engine.plan(pa, pb, SyncDirection.TWO_WAY)
    report = engine.apply(plan, snapshot_before_sync=False)

    assert report.failed == []
    assert {t.id for t in provider_yt.playlists["pa"]} == {"a1", "b1"}
    assert {t.id for t in provider_yt.playlists["pb"]} == {"a1", "b1"}
