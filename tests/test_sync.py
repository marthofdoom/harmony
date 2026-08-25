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
    actions individually rather than failing the whole group — but only the
    actions that genuinely didn't land. qo2's add fails outright (never
    landed), so the fallback must retry only it."""
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

    # One batched attempt for both (fails outright, chunk-atomic — neither
    # lands), then the fallback re-reads the (still empty) playlist and
    # retries both individually: qo1 succeeds, qo2 fails again.
    assert len(provider_qo.add_calls) == 3
    assert sorted(provider_qo.add_calls[0]) == ["qo1", "qo2"]
    assert provider_qo.add_calls[1:] == [["qo1"], ["qo2"]]
    assert [t.id for t in report.added] == ["qo1"]
    assert len(report.failed) == 1
    failed_action, message = report.failed[0]
    assert failed_action.track.id == "qo2"
    assert "qo2" in message
    assert {t.id for t in provider_qo.playlists["qo-pl"]} == {"qo1"}


def test_apply_batch_fallback_does_not_reissue_chunks_that_already_landed(db: Database) -> None:
    """Regression for bug 1's actual repro shape: a batched ``add_tracks``
    call chunks *internally* (like the real Qobuz/YouTube Music providers),
    and is not transactional — if a later internal chunk fails, earlier
    chunks already committed on the server before the exception reaches
    ``_apply_batch``. Blindly retrying every action in the group (the old
    behaviour) would re-add the tracks that already landed, duplicating them
    in the user's real playlist while ``report.added`` kept reporting the
    correct count. The fix re-reads the target playlist on the error path and
    only retries ids that are genuinely still missing."""
    src_tracks = [
        _track(id=f"yt{i}", title=f"Song {i}", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
        for i in range(3)
    ]
    dst_tracks = [
        _track(id=f"qo{i}", title=f"Song {i}", service=Service.QOBUZ, artists=["Artist"], duration_s=200)
        for i in range(3)
    ]

    provider_yt = _CountingProvider(Service.YTMUSIC)
    # add_chunk_size=1 + idempotent_add=False: each id is its own internal
    # "network request" that, once it succeeds, stays written even if a
    # later id in the same top-level add_tracks call raises — and a repeat
    # add would create a real duplicate row rather than being deduped away.
    provider_qo = _CountingProvider(
        Service.QOBUZ, catalog=dst_tracks, add_chunk_size=1, idempotent_add=False
    )
    provider_qo.fail_ids = {"qo2"}  # the last chunk fails
    provider_yt.playlists["yt-pl"] = src_tracks
    provider_qo.playlists["qo-pl"] = []

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    assert sorted(act.kind for act in plan.actions) == ["add", "add", "add"]

    report = engine.apply(plan, snapshot_before_sync=False)

    # The top-level batched call (chunks qo0, qo1 land, qo2's chunk fails),
    # then the fallback retries only qo2 — qo0/qo1 must NOT be reissued.
    assert len(provider_qo.add_calls) == 2
    assert sorted(provider_qo.add_calls[0]) == ["qo0", "qo1", "qo2"]
    assert provider_qo.add_calls[1] == ["qo2"]

    assert {t.id for t in report.added} == {"qo0", "qo1"}
    assert len(report.failed) == 1
    assert report.failed[0][0].track.id == "qo2"

    final_ids = [t.id for t in provider_qo.playlists["qo-pl"]]
    assert sorted(final_ids) == ["qo0", "qo1"]
    assert len(final_ids) == len(set(final_ids)), f"playlist has duplicates: {final_ids}"


def test_apply_batch_fallback_records_failure_when_playlist_cannot_be_reread(db: Database) -> None:
    """If the batch fails AND the target playlist can't be re-read to see
    what already landed, retrying blind would risk exactly the duplication
    bug 1 is about. The safe choice is to record every action in the batch
    as failed rather than guess."""
    yt1 = _track(id="yt1", title="Song One", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
    qo1 = _track(id="qo1", title="Song One", service=Service.QOBUZ, artists=["Artist"], duration_s=200)

    class _UnreadableAfterFailure(_CountingProvider):
        """Reads work fine until the first failed write — modelling an outage
        that breaks both the write path and the read path together, as
        opposed to the ordinary single-track failures elsewhere in this file
        (which only ever break one id, never the provider as a whole)."""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.broken = False

        def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
            self.add_calls.append(list(track_ids))
            self.broken = True
            raise RuntimeError("simulated total outage")

        def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
            if self.broken:
                raise RuntimeError("simulated: can't even read the playlist right now")
            return super().get_playlist_tracks(playlist_id)

    provider_yt = _CountingProvider(Service.YTMUSIC)
    provider_qo = _UnreadableAfterFailure(Service.QOBUZ, catalog=[qo1])
    provider_yt.playlists["yt-pl"] = [yt1]
    provider_qo.playlists["qo-pl"] = []

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    report = engine.apply(plan, snapshot_before_sync=False)

    assert report.added == []
    assert len(report.failed) == 1
    assert report.failed[0][0].track.id == "qo1"
    assert "qo1" not in {t.id for t in provider_qo.playlists["qo-pl"]}


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


# -- "low"-confidence resolution must not permanently re-prompt --------------
#
# Bug 3: forcing "manual" only for confidence == "none" left "low" links
# cached at their original ("low", <1.0) confidence. Since "low" is not in
# _CONFIDENT_ENOUGH, matching._match_one's cached-link short-circuit then
# returned "unmatched" forever, which permanently re-prompted the user,
# permanently suppressed mirror removals for that direction (has_unmatched
# stays True), and re-added a duplicate on every resolution because the
# already-present early-out only lived in the confident branch.


def _low_confidence_pair() -> tuple[Track, Track]:
    """A same-title/same-artist pair whose only disagreement is duration
    delta >= 15s (zero duration credit): title 1.0*0.5 + artist 1.0*0.35 +
    duration 0.0*0.15 = 0.85 total -> "low" (0.70 <= score < 0.88). This is
    the exact shape of the verified "Creep" 238s-vs-260s repro — an entirely
    ordinary cross-service master-length difference, not a bad match."""
    source = _track(
        id="v9", title="Creep", service=Service.YTMUSIC, artists=["Radiohead"], duration_s=238
    )
    candidate = _track(
        id="q1", title="Creep", service=Service.QOBUZ, artists=["Radiohead"], duration_s=260
    )
    return source, candidate


def test_apply_forces_manual_for_accepted_low_confidence_pick(db: Database) -> None:
    """Bug 3's core fix: accepting a "low"-confidence top candidate as-is
    must be cached as "manual"/1.0, not "low" — mirroring the existing
    "none" test, which this one is a direct sibling of."""
    source, candidate = _low_confidence_pair()

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[candidate])
    provider_yt.playlists["yt-pl"] = [source]
    provider_qo.playlists["qo-pl"] = []

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    unmatched = next(act for act in plan.actions if act.kind == "unmatched")
    assert unmatched.match.confidence == "low"  # sanity: this is the "low" shape, not "none"
    assert unmatched.match.best.track.id == candidate.id

    # User confirms the top (only) candidate as-is, exactly like clicking the
    # natural, score-descending top row in the sync UI.
    unmatched.kind = "add"
    unmatched.track = candidate

    report = engine.apply(plan, snapshot_before_sync=False)
    assert report.failed == []

    link = db.get_link(Service.YTMUSIC, "v9", Service.QOBUZ)
    assert link is not None
    assert link["confidence"] == "manual"
    assert link["score"] == 1.0


def test_low_confidence_resolution_does_not_re_prompt_or_duplicate_on_next_sync(db: Database) -> None:
    """Full regression for bug 3's three compounding consequences: once a
    "low" match is resolved, the *next* plan()/apply() cycle must not (a)
    re-flag it as unmatched, (b) re-add a duplicate, or (c) permanently
    suppress mirror removals for the direction because of it."""
    source, candidate = _low_confidence_pair()
    orphan = _track(id="qo-orphan", title="Truly Orphaned", service=Service.QOBUZ, artists=["Nobody"], duration_s=50)

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[candidate])
    provider_yt.playlists["yt-pl"] = [source]
    provider_qo.playlists["qo-pl"] = [orphan]

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    # -- run 1: unmatched, user resolves to the top (only) candidate.
    plan1 = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    unmatched = next(act for act in plan1.actions if act.kind == "unmatched")
    unmatched.kind = "add"
    unmatched.track = candidate
    report1 = engine.apply(plan1, snapshot_before_sync=False)
    assert report1.failed == []
    assert {t.id for t in provider_qo.playlists["qo-pl"]} == {"q1", "qo-orphan"}

    # -- run 2: must be fully resolved now — no unmatched, no re-add, and the
    # genuinely-orphaned track must be removable again (not permanently
    # protected by a stale has_unmatched).
    plan2 = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    assert [act.kind for act in plan2.actions] == ["remove"]
    assert plan2.actions[0].track.id == "qo-orphan"
    assert not plan2.notes, "removals must no longer be suppressed once the low match is resolved"

    report2 = engine.apply(plan2, snapshot_before_sync=False)
    assert report2.failed == []
    assert [t.id for t in report2.added] == []
    assert [t.id for t in report2.removed] == ["qo-orphan"]

    final_ids = [t.id for t in provider_qo.playlists["qo-pl"]]
    assert final_ids == ["q1"], f"q1 must not be duplicated across syncs: {final_ids}"

    # -- run 3: fully stable, nothing left to do.
    plan3 = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    assert plan3.actions == []


def test_low_confidence_match_already_present_in_target_needs_no_action(db: Database) -> None:
    """The already-present early-out must apply to "low"-confidence results
    too, not just confident ones: if the top candidate is already sitting in
    the target playlist, there is nothing to add and no reason to prompt the
    user — prompting here is exactly what let the sync UI's default "use top
    candidate" resolution silently re-add (duplicate) a track that was
    already correctly mirrored."""
    source, candidate = _low_confidence_pair()

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[candidate])
    provider_yt.playlists["yt-pl"] = [source]
    # The target already has the (correct) counterpart — e.g. from a manual
    # add, or a previous sync using a different code path.
    provider_qo.playlists["qo-pl"] = [candidate]

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    assert plan.actions == []
    assert not plan.notes

    report = engine.apply(plan, snapshot_before_sync=False)
    assert report.added == []
    assert report.removed == []
    assert [t.id for t in provider_qo.playlists["qo-pl"]] == ["q1"], "must not be duplicated"


def test_none_confidence_already_present_coincidence_still_flagged_unmatched(db: Database) -> None:
    """Contrast case: a "none"-confidence best guess must NOT get the
    already-present bypass just because its id happens to coincide with an
    existing target track — "none" means no real candidate was found, so
    that coincidence isn't trustworthy evidence of anything. (This is also
    the shape that keeps ``test_mirror_no_action_when_already_present_and_
    suppresses_removal_when_unmatched`` meaningful: a degenerate single-item
    catalog forces an unrelated track to "match" the one item present.)"""
    unrelated_source = _track(
        id="v-noise", title="Totally Different", service=Service.YTMUSIC, artists=["Nobody"], duration_s=5
    )
    already_present = _track(
        id="q1", title="Some Song", service=Service.QOBUZ, artists=["Someone"], duration_s=500
    )

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = FakeProvider(Service.QOBUZ, catalog=[already_present])
    provider_yt.playlists["yt-pl"] = [unrelated_source]
    provider_qo.playlists["qo-pl"] = [already_present]

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    assert [act.kind for act in plan.actions] == ["unmatched"]
    assert plan.actions[0].match.confidence == "none"


# -- cancellation and progress granularity during apply() --------------------


def test_apply_honours_cancel_between_sub_batches_not_just_once(db: Database) -> None:
    """Bug 2: apply() used to check the cancel token once per (service,
    playlist, kind) group — at most four checks for an entire sync — so a
    large single-group plan cancelled shortly after starting ran to
    completion anyway. It must be checked between sync.py's own sub-chunks
    of a group too."""
    from harmony.tasks import Cancelled, CancelToken

    n = 50
    src_tracks = [
        _track(id=f"yt{i}", title=f"Song {i}", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
        for i in range(n)
    ]
    dst_tracks = [
        _track(id=f"qo{i}", title=f"Song {i}", service=Service.QOBUZ, artists=["Artist"], duration_s=200)
        for i in range(n)
    ]

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = _CountingProvider(Service.QOBUZ, catalog=dst_tracks)
    provider_yt.playlists["yt-pl"] = src_tracks
    provider_qo.playlists["qo-pl"] = []

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    assert len(plan.actions) == n

    cancel = CancelToken()
    ticks: list[float] = []

    def on_progress(frac: float, _msg: str) -> None:
        ticks.append(frac)
        if len(ticks) == 1:
            cancel.cancel()

    with pytest.raises(Cancelled):
        engine.apply(plan, progress=on_progress, cancel=cancel, snapshot_before_sync=False)

    # Cancellation fired right after the first sub-batch's progress tick, so
    # apply() must have stopped there instead of running the remaining
    # sub-batches to completion — the old bug was precisely that nothing
    # between "start" and "done" ever looked at the token, so a plan this
    # size ran to completion regardless of when cancel() was called.
    assert ticks == [pytest.approx(0.4)], f"expected exactly one tick before the raise: {ticks}"
    assert len(provider_qo.playlists["qo-pl"]) < n, "apply ran to completion despite being cancelled"
    assert not any(provider_qo.add_calls) or len(provider_qo.add_calls[0]) < n, (
        "the very first provider call must not already cover the whole plan"
    )
    # And every provider call it did make was sync.py's own sub-chunk size,
    # not the whole 50-track group in one shot — that's what makes a
    # mid-run cancel checkpoint possible at all.
    assert all(len(call) <= 20 for call in provider_qo.add_calls)


def test_apply_progress_ticks_more_than_once_for_a_large_uncancelled_batch(db: Database) -> None:
    """Even without cancellation, a large single-group batch must report
    progress in more than one jump so a progress bar isn't frozen for the
    whole run."""
    n = 50
    src_tracks = [
        _track(id=f"yt{i}", title=f"Song {i}", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
        for i in range(n)
    ]
    dst_tracks = [
        _track(id=f"qo{i}", title=f"Song {i}", service=Service.QOBUZ, artists=["Artist"], duration_s=200)
        for i in range(n)
    ]

    provider_yt = FakeProvider(Service.YTMUSIC)
    provider_qo = FakeProvider(Service.QOBUZ, catalog=dst_tracks)
    provider_yt.playlists["yt-pl"] = src_tracks
    provider_qo.playlists["qo-pl"] = []

    engine = SyncEngine({Service.YTMUSIC: provider_yt, Service.QOBUZ: provider_qo}, db)
    a = Playlist(id="yt-pl", title="Mix", service=Service.YTMUSIC)
    b = Playlist(id="qo-pl", title="Mix", service=Service.QOBUZ)

    plan = engine.plan(a, b, SyncDirection.MIRROR_A_TO_B)
    ticks: list[float] = []
    report = engine.apply(plan, progress=lambda frac, msg: ticks.append(frac), snapshot_before_sync=False)

    assert len(report.added) == n
    assert len(ticks) >= 2, f"a {n}-track sync must report more than one progress tick: {ticks}"
    assert ticks[-1] == 1.0
    assert ticks == sorted(ticks)
