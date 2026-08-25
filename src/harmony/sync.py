"""Sync engine: turns two playlists (possibly on different services) into a
plan of add/remove/unmatched actions, and applies that plan through the
providers.

Planning is pure — it only calls read-only provider methods (``search``,
``get_playlist_tracks``) — so the UI can preview a sync, let the user resolve
ambiguous matches, and only then call :meth:`SyncEngine.apply`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

from . import io_formats
from .errors import ProviderError
from .matching import HIGH_THRESHOLD, LOW_THRESHOLD, MatchResult, match_tracks
from .models import Playlist, Service, Track
from .providers.base import _chunked

if TYPE_CHECKING:
    from collections.abc import Callable

    from .db import Database
    from .providers.base import MusicProvider
    from .tasks import CancelToken

log = logging.getLogger(__name__)

# Confidences that mean we KNOW the counterpart: a fresh ISRC-verified or
# high-scoring fuzzy search match, or a link previously written because a
# person manually confirmed it (see ``SyncEngine._record_link``). This is a
# statement about knowledge, not consent — see ``SyncEngine._plan_one_direction``
# and its "RESOLVED_MISSING" branch for how consent (``auto_accept_high``) is
# applied separately, on top of this. "low" and "none" mean we do NOT know
# the counterpart; they must never drive an add or suppress a remove on
# their own — that is precisely the data-loss bug this module guards
# against. Always use this module constant for "is this confidence
# trustworthy" checks in this file, never a derived/instance-level set —
# see docs/ARCHITECTURE.md's sync section for why.
_CONFIDENT_ENOUGH = {"exact", "high", "manual"}

# Sync-level sub-chunking for apply()'s per-group provider calls, so a large
# group has more than one cancellation checkpoint and more than one progress
# tick instead of regressing to one provider call per track (see
# ``SyncEngine.apply``). Both sizes are on top of, and independent from,
# whatever chunk size a provider uses internally for its own network
# requests (YT adds 100, Qobuz adds 50 — see ``_apply_batch``'s docstring).
#
# The two kinds are NOT symmetric, though, so they get different sizes:
# "add" keeps a small chunk (20) purely for cancellation/progress
# responsiveness. "remove" needs a much larger chunk because
# ``providers/ytmusic.py``'s ``remove_tracks`` does a full playlist re-fetch
# per call — at chunk size 20, removing 500 tracks would mean 25 full
# playlist downloads instead of ~3 (500 / 200, rounded up). A large-but-not-
# unbounded remove chunk keeps that cost down while still leaving more than
# one checkpoint for a very large removal.
_APPLY_ADD_CHUNK_SIZE = 20
_APPLY_REMOVE_CHUNK_SIZE = 200


def _apply_chunk_size(kind: str) -> int:
    return _APPLY_ADD_CHUNK_SIZE if kind == "add" else _APPLY_REMOVE_CHUNK_SIZE


class SyncDirection(Enum):
    MIRROR_A_TO_B = auto()
    MIRROR_B_TO_A = auto()
    TWO_WAY = auto()


@dataclass(slots=True)
class SyncAction:
    """One thing the plan wants to happen. ``kind`` is "add" | "remove" | "unmatched"."""

    kind: str
    target: Service
    track: Track
    match: MatchResult | None
    # Which playlist on ``target`` this action applies to. Needed because two
    # actions can share the same ``target`` service while pointing at two
    # different playlists (e.g. a TWO_WAY sync between two playlists on the
    # same service) — grouping/looking up by service alone silently merges
    # them. Defaults to "" only so hand-built SyncActions (tests, or older
    # callers) don't break; planning always fills it in.
    target_playlist_id: str = ""
    # Only meaningful on ``kind == "add"``. True means: we KNOW the
    # counterpart (confidence is in ``_CONFIDENT_ENOUGH``) but the match is
    # only "high" — not "exact" or "manual" — and ``auto_accept_high`` is
    # False, so writing it without asking would defeat that setting.
    # ``auto_accept_high`` is a question about CONSENT to write a known
    # match, never about whether the match is known, so this flag is
    # orthogonal to classification: it never makes an action "unmatched" and
    # never suppresses removals (see ``SyncEngine._plan_one_direction``).
    # ``SyncEngine.apply`` will not write an action with this flag set; a
    # caller that has obtained confirmation clears it before calling
    # ``apply`` (mirroring how the sync UI already flips an "unmatched"
    # action's ``kind`` in place after resolution — see ``SyncPlan.normalise``).
    needs_confirmation: bool = False


@dataclass(slots=True)
class SyncPlan:
    source: Playlist
    target: Playlist
    actions: list[SyncAction]
    notes: list[str] = field(default_factory=list)
    # The target-side track ids that were already present, per (target
    # service, target playlist id), at the moment this plan was built —
    # captured once per direction while planning (``dst_ids`` inside
    # ``SyncEngine._plan_one_direction``). ``SyncEngine.apply`` uses this as
    # the source of truth for "would this add be a duplicate", instead of
    # re-reading the provider or relying on classification to have filtered
    # duplicates out. This is what makes it safe for classification to never
    # special-case "already present" itself: an "unmatched" row the UI
    # resolves onto a candidate that happens to already be in the target is
    # still caught, at apply time, without an extra provider read. Defaults
    # to empty so hand-built ``SyncPlan``s (tests, older callers) simply get
    # no duplicate protection rather than breaking.
    target_track_ids: dict[tuple[Service, str], frozenset[str]] = field(default_factory=dict)

    def summary(self) -> str:
        adds = sum(1 for a in self.actions if a.kind == "add")
        removes = sum(1 for a in self.actions if a.kind == "remove")
        unmatched = sum(1 for a in self.actions if a.kind == "unmatched")
        text = f"{adds} to add, {removes} to remove, {unmatched} unmatched"
        if self.notes:
            text += " — " + "; ".join(self.notes)
        return text

    def normalise(self) -> None:
        """Drop stale ``remove`` actions that a resolved ``add`` supersedes.

        The sync UI resolves an "unmatched" row by flipping its ``kind`` to
        "add" in place (see ``ui/sync_page.py``'s ``_use_candidate``). If the
        plan separately contains a ``remove`` for that very target track —
        because, before resolution, the planner saw it as an unaccounted-for
        target-side track — both actions would survive into ``apply``, which
        runs adds before removes: the track gets added back and then
        immediately deleted. This is called unconditionally by ``apply`` so
        the guarantee holds regardless of what the UI (or any other caller)
        does to a plan's actions before applying it.
        """
        add_keys = {
            (a.target, a.target_playlist_id, a.track.id) for a in self.actions if a.kind == "add"
        }
        self.actions = [
            a
            for a in self.actions
            if not (a.kind == "remove" and (a.target, a.target_playlist_id, a.track.id) in add_keys)
        ]


@dataclass(slots=True)
class SyncReport:
    added: list[Track] = field(default_factory=list)
    removed: list[Track] = field(default_factory=list)
    skipped: list[SyncAction] = field(default_factory=list)
    failed: list[tuple[SyncAction, str]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def _wrap_progress(
    progress: Callable[[float, str], None] | None, cancel: CancelToken | None
) -> Callable[[float, str], None] | None:
    """Fold cancellation checking into a progress callback.

    ``matching.match_tracks`` calls ``progress`` once per track, which makes
    it a convenient place to hook a cancel check between units of work — the
    cooperative-cancellation contract described in ``tasks.CancelToken``.
    """
    if progress is None and cancel is None:
        return None

    def cb(frac: float, msg: str) -> None:
        if cancel is not None:
            cancel.raise_if_cancelled()
        if progress is not None:
            progress(frac, msg)

    return cb


def _split_progress(
    progress: Callable[[float, str], None] | None,
) -> tuple[Callable[[float, str], None] | None, Callable[[float, str], None] | None]:
    """Divide a 0..1 progress callback into two consecutive halves."""
    if progress is None:
        return None, None

    def make(offset: float) -> Callable[[float, str], None]:
        def cb(frac: float, msg: str) -> None:
            progress(offset + frac * 0.5, msg)

        return cb

    return make(0.0), make(0.5)


class SyncEngine:
    def __init__(
        self,
        providers: dict[Service, MusicProvider],
        db: Database,
        *,
        high_threshold: float = HIGH_THRESHOLD,
        low_threshold: float = LOW_THRESHOLD,
        auto_accept_high: bool = True,
    ) -> None:
        """``high_threshold``/``low_threshold``/``auto_accept_high`` come from
        ``Settings``; they are passed in rather than read here so the engine
        stays independent of ``harmony.config`` (see docs/ARCHITECTURE.md).

        ``auto_accept_high`` False means only ISRC-verified (``exact``) and
        user-resolved (``manual``) matches are written without asking. A
        "high" fuzzy match is still *known* (it still counts as a resolved
        match for classification, and still protects an already-mirrored
        target track from removal) but a not-yet-present one is marked
        ``SyncAction.needs_confirmation`` instead of being written straight
        away — the conservative choice for anyone who has been bitten by a
        confident-looking wrong match, without the two failure modes
        documented on ``_CONFIDENT_ENOUGH`` and ``_plan_one_direction``.

        Deliberately NOT exposed as a set-of-confidences property the way an
        earlier version of this engine did: consent-to-write and knowledge-
        of-a-match are different questions, and collapsing them back into
        one property is what caused those failure modes. See
        docs/ARCHITECTURE.md's sync section.
        """
        self.providers = providers
        self.db = db
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        self.auto_accept_high = auto_accept_high

    def _provider_for(self, service: Service) -> MusicProvider:
        provider = self.providers.get(service)
        if provider is None:
            raise ProviderError(f"No provider configured for {service.value}")
        return provider

    # -- planning ------------------------------------------------------

    def plan(
        self,
        a: Playlist,
        b: Playlist,
        direction: SyncDirection,
        *,
        progress: Callable[[float, str], None] | None = None,
        cancel: CancelToken | None = None,
    ) -> SyncPlan:
        provider_a, provider_b = self._provider_for(a.service), self._provider_for(b.service)

        if direction is SyncDirection.MIRROR_A_TO_B:
            actions, note, key, dst_ids = self._plan_one_direction(
                a, provider_a, b, provider_b, allow_remove=True, progress=progress, cancel=cancel
            )
            return SyncPlan(
                source=a, target=b, actions=actions, notes=[note] if note else [], target_track_ids={key: dst_ids}
            )

        if direction is SyncDirection.MIRROR_B_TO_A:
            actions, note, key, dst_ids = self._plan_one_direction(
                b, provider_b, a, provider_a, allow_remove=True, progress=progress, cancel=cancel
            )
            return SyncPlan(
                source=b, target=a, actions=actions, notes=[note] if note else [], target_track_ids={key: dst_ids}
            )

        if direction is SyncDirection.TWO_WAY:
            progress_ab, progress_ba = _split_progress(progress)
            actions_ab, note_ab, key_ab, dst_ids_ab = self._plan_one_direction(
                a, provider_a, b, provider_b, allow_remove=False, progress=progress_ab, cancel=cancel
            )
            actions_ba, note_ba, key_ba, dst_ids_ba = self._plan_one_direction(
                b, provider_b, a, provider_a, allow_remove=False, progress=progress_ba, cancel=cancel
            )
            notes = [n for n in (note_ab, note_ba) if n]
            return SyncPlan(
                source=a,
                target=b,
                actions=[*actions_ab, *actions_ba],
                notes=notes,
                target_track_ids={key_ab: dst_ids_ab, key_ba: dst_ids_ba},
            )

        raise ValueError(f"Unknown sync direction: {direction!r}")

    def _plan_one_direction(
        self,
        src_playlist: Playlist,
        src_provider: MusicProvider,
        dst_playlist: Playlist,
        dst_provider: MusicProvider,
        *,
        allow_remove: bool,
        progress: Callable[[float, str], None] | None,
        cancel: CancelToken | None,
    ) -> tuple[list[SyncAction], str | None, tuple[Service, str], frozenset[str]]:
        """Classify every source track exactly once into one of three states.

        RESOLVED_PRESENT — we know the counterpart (``result.confidence`` is
        in ``_CONFIDENT_ENOUGH``) and it is already in the target. No action;
        its id is recorded in ``matched_dst_ids`` so the removal loop below
        does not treat it as an orphan.

        RESOLVED_MISSING — we know the counterpart and it is not in the
        target. Emits an "add". ``needs_confirmation`` is set when the only
        reason we know it is a "high" fuzzy score and ``auto_accept_high``
        is False — that governs CONSENT to write it without asking, a
        question entirely separate from whether it's known, so it never
        affects ``has_undetermined`` below.

        UNDETERMINED — we do not know the counterpart: confidence is below
        the auto-accept bar ("low"/"none") or there is no candidate at all.
        Emits an "unmatched" row for the UI to resolve, and sets
        ``has_undetermined``, which suppresses every removal for this
        direction (see below) — a removal means "this target track has no
        counterpart in the source", which is unknowable while any source
        track's match is still undetermined.

        Both RESOLVED states are "known" purely by ``result.confidence in
        _CONFIDENT_ENOUGH`` — the module constant, deliberately NOT
        ``self._confident_enough`` (an ``auto_accept_high``-aware property
        this class no longer defines for classification purposes). Gating
        classification on consent-to-write would make an already-mirrored
        "high" match undetermined whenever ``auto_accept_high`` is False,
        which reintroduces exactly the compounding failure this docstring's
        three-state model exists to rule out: a real match relabelled as
        "unknown" suppresses every removal in the direction, forever, for as
        long as the setting is off.

        Duplicate adds (a RESOLVED_MISSING id, or a UI-resolved UNDETERMINED
        id, that turns out to already be in the target by the time
        ``apply()`` runs) are deliberately NOT handled here. See
        ``SyncPlan.target_track_ids`` and ``SyncEngine.apply``.
        """
        if cancel is not None:
            cancel.raise_if_cancelled()
        src_tracks = src_provider.get_playlist_tracks(src_playlist.id)
        dst_tracks = dst_provider.get_playlist_tracks(dst_playlist.id)
        dst_ids = {t.id for t in dst_tracks}

        results = match_tracks(
            src_tracks,
            dst_provider,
            progress=_wrap_progress(progress, cancel),
            db=self.db,
            high_threshold=self.high_threshold,
            low_threshold=self.low_threshold,
        )

        actions: list[SyncAction] = []
        # Dst tracks accounted for by a RESOLVED_PRESENT source track — must
        # not be treated as orphans by the removal loop below.
        matched_dst_ids: set[str] = set()
        has_undetermined = False
        for result in results:
            best = result.best
            if best is not None and result.confidence in _CONFIDENT_ENOUGH:
                if best.track.id in dst_ids:
                    # RESOLVED_PRESENT.
                    matched_dst_ids.add(best.track.id)
                    continue
                # RESOLVED_MISSING.
                needs_confirmation = result.confidence == "high" and not self.auto_accept_high
                matched_dst_ids.add(best.track.id)
                actions.append(
                    SyncAction(
                        kind="add",
                        target=dst_provider.service,
                        track=best.track,
                        match=result,
                        target_playlist_id=dst_playlist.id,
                        needs_confirmation=needs_confirmation,
                    )
                )
                continue
            # UNDETERMINED: don't guess, let the UI ask. A failed or
            # low-confidence match here does NOT mean the target has no
            # counterpart — it means we couldn't find one.
            has_undetermined = True
            actions.append(
                SyncAction(
                    kind="unmatched",
                    target=dst_provider.service,
                    track=result.source,
                    match=result,
                    target_playlist_id=dst_playlist.id,
                )
            )

        note: str | None = None
        if allow_remove:
            if cancel is not None:
                cancel.raise_if_cancelled()
            if has_undetermined:
                # Safest correct behaviour: never delete a target track on the
                # basis of a source track whose match outcome we don't trust
                # yet. Suppress ALL removals for this direction rather than
                # guess which ones would have been safe, and say why.
                undetermined_count = sum(1 for a in actions if a.kind == "unmatched")
                note = (
                    f"removals to {dst_playlist.title!r} skipped: {undetermined_count} source "
                    "track(s) unmatched — resolve them, then re-preview, before deleting anything"
                )
            else:
                for track in dst_tracks:
                    # Never remove a target track that some source track's
                    # match — RESOLVED_PRESENT or RESOLVED_MISSING — accounted
                    # for. (This loop only runs when ``has_undetermined`` is
                    # False, i.e. every result landed in one of those two
                    # states or matched nothing at all, so ``matched_dst_ids``
                    # alone is the complete set.)
                    if track.id in matched_dst_ids:
                        continue
                    actions.append(
                        SyncAction(
                            kind="remove",
                            target=dst_provider.service,
                            track=track,
                            match=None,
                            target_playlist_id=dst_playlist.id,
                        )
                    )
        return actions, note, (dst_provider.service, dst_playlist.id), frozenset(dst_ids)

    # -- applying ------------------------------------------------------

    def apply(
        self,
        plan: SyncPlan,
        *,
        progress: Callable[[float, str], None] | None = None,
        cancel: CancelToken | None = None,
        snapshot_before_sync: bool = True,
    ) -> SyncReport:
        report = SyncReport()

        if snapshot_before_sync:
            self._snapshot(plan.source)
            self._snapshot(plan.target)
        if cancel is not None:
            cancel.raise_if_cancelled()

        # Always run: defends against a stale ``remove`` surviving a UI
        # resolution of the same track, regardless of what mutated the plan
        # between preview and apply.
        plan.normalise()

        playlists_by_id: dict[tuple[Service, str], Playlist] = {
            (plan.source.service, plan.source.id): plan.source,
            (plan.target.service, plan.target.id): plan.target,
        }

        # Split "add"/"remove" actions into what actually gets sent to a
        # provider ("actionable") and what apply() withholds on its own
        # authority, never silently — every withheld action ends up in
        # ``report.skipped``, the same as an "unmatched" one:
        #
        #  - a duplicate add: its track id was already present in the target
        #    at plan time (``plan.target_track_ids``, captured once per
        #    direction while planning — see ``_plan_one_direction``). This is
        #    what actually prevents a duplicate add, including the case
        #    where the UI resolves an "unmatched" row onto a candidate that
        #    turns out to already be in the target — without an extra
        #    provider read, and without classification having to special-
        #    case "already present" and risk dropping a track instead (the
        #    defect this file was fixed for).
        #  - an unconfirmed "high" add: ``needs_confirmation`` is set at plan
        #    time when the match is known but not "exact"/"manual", and
        #    ``auto_accept_high`` is False. Writing it here anyway would
        #    silently defeat that setting. A caller that has obtained
        #    confirmation clears the flag on the action before calling
        #    ``apply`` (mirroring how the sync UI already flips an
        #    "unmatched" action's ``kind`` in place after resolution).
        actionable: list[SyncAction] = []
        withheld: list[SyncAction] = []
        for action in plan.actions:
            if action.kind == "remove":
                actionable.append(action)
            elif action.kind == "add":
                known = plan.target_track_ids.get((action.target, action.target_playlist_id), frozenset())
                if action.track.id in known or action.needs_confirmation:
                    withheld.append(action)
                else:
                    actionable.append(action)
            # "unmatched" actions are neither applied nor withheld here —
            # they're reported below, same as always.

        total = len(actionable) or 1
        done_count = 0

        for key, batch in _group_actions(actionable).items():
            if cancel is not None:
                cancel.raise_if_cancelled()
            service, playlist_id, kind = key
            playlist = playlists_by_id.get((service, playlist_id))
            if playlist is None:
                message = f"No playlist known for service {service.value} / {playlist_id}"
                for action in batch:
                    log.warning("Sync action %s(%s) failed: %s", action.kind, action.track.title, message)
                    report.failed.append((action, message))
                done_count += len(batch)
                if progress is not None:
                    progress(done_count / total, batch[-1].track.title)
                continue

            known_before = plan.target_track_ids.get((service, playlist_id), frozenset())

            # Sub-chunk each group so cancellation is honoured and progress
            # moves in more than one jump. A single provider call per whole
            # group (potentially hundreds of tracks) used to mean at most one
            # cancel check and one progress tick per group — a 50-track plan
            # cancelled right after it started ran to completion because
            # nothing between "start" and "done" ever looked at the token.
            # See ``_apply_chunk_size`` for why the size differs by kind.
            for sub in _chunked(batch, _apply_chunk_size(kind)):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                self._apply_batch(kind, service, playlist, sub, report, known_before=known_before, cancel=cancel)
                done_count += len(sub)
                if progress is not None:
                    progress(done_count / total, sub[-1].track.title)

        report.skipped.extend(a for a in plan.actions if a.kind == "unmatched")
        report.skipped.extend(withheld)
        unconfirmed_count = sum(1 for a in withheld if a.needs_confirmation)
        if unconfirmed_count:
            report.messages.append(
                f"{unconfirmed_count} add(s) await confirmation (high-confidence match, "
                "auto-accept disabled) and were not written"
            )
        return report

    def _apply_batch(
        self,
        kind: str,
        service: Service,
        playlist: Playlist,
        batch: list[SyncAction],
        report: SyncReport,
        *,
        known_before: frozenset[str] = frozenset(),
        cancel: CancelToken | None = None,
    ) -> None:
        """Apply one (service, playlist, kind) sub-batch as a single provider call.

        Batching lets the provider's own chunking do its job instead of
        issuing one network round-trip (and, for providers like YouTube Music
        whose ``remove_tracks`` re-fetches the whole playlist, one full
        re-fetch) per track. Real providers chunk internally and are *not*
        transactional (``providers/ytmusic.py`` loops chunks of 100,
        ``providers/qobuz.py`` chunks of 50): if chunk *k* of an
        ``add_tracks``/``remove_tracks`` call fails, chunks ``1..k-1`` have
        already landed on the server before the exception reaches us. Blindly
        retrying every action in the batch individually — the old
        behaviour — would re-add tracks that already made it through,
        duplicating them in the user's playlist while ``report.added`` kept
        reporting the correct count. See ``_apply_batch_fallback``.

        ``known_before`` is ``plan.target_track_ids`` for this (service,
        playlist) — the ids present at plan time, before this run wrote
        anything — threaded through to ``_apply_batch_fallback`` for its own
        staleness defence. It plays no part in the happy path: ``apply()``
        has already filtered every action whose id was already present at
        plan time out of what reaches here at all (see ``apply``'s
        docstring-level comment on "actionable"/"withheld").
        """
        provider = self._provider_for(service)
        track_ids = [action.track.id for action in batch]
        try:
            if kind == "add":
                provider.add_tracks(playlist.id, track_ids)
            else:
                provider.remove_tracks(playlist.id, track_ids)
        except Exception as exc:  # noqa: BLE001 - fall back to per-action isolation below
            log.warning(
                "Batched %s of %d track(s) on %s failed (%s); retrying individually",
                kind, len(batch), playlist.id, exc,
            )
            self._apply_batch_fallback(kind, provider, playlist, batch, report, exc, known_before=known_before, cancel=cancel)
            return

        for action in batch:
            if kind == "add":
                report.added.append(action.track)
                self._record_link(action)
            else:
                report.removed.append(action.track)

    def _apply_batch_fallback(
        self,
        kind: str,
        provider: MusicProvider,
        playlist: Playlist,
        batch: list[SyncAction],
        report: SyncReport,
        exc: Exception,
        *,
        known_before: frozenset[str] = frozenset(),
        cancel: CancelToken | None = None,
    ) -> None:
        """Retry a failed batch without risking duplicate writes.

        A failed batched call may have partially landed (see
        ``_apply_batch``'s docstring), so before retrying anything we re-read
        the target playlist's *current* track ids — one extra fetch, only on
        this error path — and use it to retry only what genuinely still needs
        doing: for "add", skip ids that are already present — they landed as
        part of this run's own partially-failed batch. (Ids that were
        already present *before* this run started never reach this method at
        all: ``apply()`` filters those out of the action list up front using
        ``plan.target_track_ids``, the same ids passed in here as
        ``known_before``.) For "remove", skip ids that are already gone.

        If the playlist can't be re-read, we have no way to tell what already
        landed, so retrying blind would risk exactly the duplication this
        exists to prevent. In that case every action in the batch is recorded
        as failed instead — a recorded failure the user can retry beats a
        silently corrupted playlist.

        Residual risk (documented, not fully closed here): this re-read is
        trusted as ground truth. A stale replica or a paginated read that
        silently truncates can under-report what is actually there — we have
        verified this concretely producing a real duplicate (final ids
        ``['q0','q1','q0','q1']`` against a ``report.added`` of only
        ``['q0','q1']``) with a provider double that models exactly that.
        ``known_before`` gives a cheap partial defence: it is unioned into
        the presence check as a floor, so a read that drops rows which were
        already there before this run cannot make apply() think a
        pre-existing track needs re-adding. It does NOT protect the ids this
        run itself is trying to add right now — those were never in
        ``known_before`` — so a stale read during *this* failure path can
        still fail to see one of them and cause a genuine duplicate. Closing
        that gap needs provider support (an idempotent add endpoint, or a
        read-after-write guarantee) that is out of this module's reach.
        """
        try:
            current_ids = {t.id for t in provider.get_playlist_tracks(playlist.id)}
        except Exception as read_exc:  # noqa: BLE001 - see docstring: fail closed
            message = (
                f"batched {kind} failed ({exc}) and the playlist could not be re-read "
                f"to retry safely ({read_exc})"
            )
            log.error(
                "Batched %s of %d track(s) on %s failed and could not be verified; "
                "recording as failed rather than risking duplication: %s",
                kind, len(batch), playlist.id, read_exc,
            )
            for action in batch:
                report.failed.append((action, message))
            return

        if kind == "add" and len(current_ids) < len(known_before):
            log.warning(
                "Re-read of %s after a failed add returned fewer tracks (%d) than were known "
                "present before this sync started (%d) -- the read may be stale or paginated; "
                "using the plan's captured ids as a floor to avoid mistaking a still-present "
                "track for missing.",
                playlist.id, len(current_ids), len(known_before),
            )
        # See the residual-risk paragraph above: this floor only protects
        # ids known present *before* this run, not ids this run itself just
        # (maybe) wrote.
        presence_ids = current_ids | known_before if kind == "add" else current_ids

        for action in batch:
            if cancel is not None:
                cancel.raise_if_cancelled()
            already_present = action.track.id in presence_ids
            if kind == "add" and already_present:
                # Already landed as part of this run's own partially-failed
                # batch — count it without writing it again.
                report.added.append(action.track)
                self._record_link(action)
                continue
            if kind == "remove" and not already_present:
                # Already gone — nothing left to do.
                report.removed.append(action.track)
                continue
            self._apply_one(action, provider, playlist, report)

    def _apply_one(
        self,
        action: SyncAction,
        provider: MusicProvider,
        playlist: Playlist,
        report: SyncReport,
    ) -> None:
        """Perform one add/remove. Any failure is swallowed into ``report.failed``
        so a single flaky provider call cannot abort the rest of a batch."""
        try:
            if action.kind == "add":
                provider.add_tracks(playlist.id, [action.track.id])
                report.added.append(action.track)
                self._record_link(action)
            elif action.kind == "remove":
                provider.remove_tracks(playlist.id, [action.track.id])
                report.removed.append(action.track)
        except Exception as exc:  # noqa: BLE001 - isolate one bad action from the rest
            log.warning("Sync action %s(%s) failed: %s", action.kind, action.track.title, exc)
            report.failed.append((action, str(exc)))

    def _record_link(self, action: SyncAction) -> None:
        """Cache the match backing a successful "add" for next time.

        If the track actually applied differs from ``action.match.best`` —
        the top-ranked search candidate — a person resolved this action by
        hand (the sync UI's "Use this" flow flips an "unmatched" action's
        ``kind``/``track`` in place without touching ``match``). That is
        stronger evidence than any fuzzy score, so it is cached as a
        "manual" link at score 1.0 rather than mis-attributing the top
        candidate's score to the track the user actually picked.

        The same applies whenever the *accepted* match's own confidence is
        not already in ``_CONFIDENT_ENOUGH`` — not just "none" — even if the
        applied id happens to equal ``best``'s id. "low" is an entirely
        ordinary cross-service scoring outcome (e.g. a 22s master-length
        difference), and the sync UI's natural click is the top-ranked
        candidate, which *is* ``match.best``. If a "low" link were cached
        verbatim at its original confidence, ``matching._match_one`` would
        keep returning it as authoritative forever, "low" is not in
        ``_CONFIDENT_ENOUGH``, so the action would be "unmatched" again on
        every subsequent sync — which the removal guard then treats as
        permanently undetermined, suppressing all removals for that
        direction forever, on top of re-prompting the user for the same
        track every single time. Treating the user's acceptance (of any
        confidence not already known-enough) as "manual" is what breaks that
        loop: once cached as "manual", it lands in the RESOLVED_PRESENT
        branch of ``_plan_one_direction`` next time (or RESOLVED_MISSING, if
        it isn't there yet), and ``apply()``'s duplicate guard —
        ``plan.target_track_ids``, not classification — is what stops a
        second add from actually being written if it already is.

        Using the module constant ``_CONFIDENT_ENOUGH`` here (rather than a
        locally hardcoded tuple, or ``self``-scoped ``auto_accept_high``
        logic) matters for a second reason: a user-confirmed "high" pick —
        reachable when ``auto_accept_high`` is False and the user clears
        ``needs_confirmation`` on an otherwise-unmodified action — must be
        recorded as authoritative at its true confidence, not silently
        forced to "manual" just because a hand-maintained tuple here forgot
        to agree with the constant classification already uses.
        """
        if self.db is None or action.match is None or action.match.best is None:
            return
        src = action.match.source
        if action.track.id != action.match.best.track.id or action.match.confidence not in _CONFIDENT_ENOUGH:
            confidence, score_value = "manual", 1.0
        else:
            confidence, score_value = action.match.confidence, action.match.best.score
        self.db.put_link(
            src.service, src.id, action.track.service, action.track.id, score_value, confidence
        )

    def _snapshot(self, playlist: Playlist) -> None:
        if self.db is None:
            # Nowhere to put it. Losing the safety net is bad, but crashing
            # part-way through apply() — after some writes have already
            # landed — would be worse than proceeding without a snapshot.
            log.warning("No database available; skipping pre-sync snapshot of %s", playlist.id)
            return
        provider = self._provider_for(playlist.service)
        tracks = provider.get_playlist_tracks(playlist.id)
        payload = {
            "playlist": io_formats.playlist_to_dict(playlist),
            "tracks": [io_formats.track_to_dict(t) for t in tracks],
        }
        self.db.save_snapshot(playlist.service, playlist.id, payload)

    # -- cloning ------------------------------------------------------

    def clone_playlist(
        self,
        src: Playlist,
        dst_service: Service,
        *,
        progress: Callable[[float, str], None] | None = None,
        cancel: CancelToken | None = None,
    ) -> SyncPlan:
        """Create an empty playlist on ``dst_service`` and plan filling it from ``src``."""
        dst_provider = self._provider_for(dst_service)
        new_playlist = dst_provider.create_playlist(src.title, description=src.description, public=src.public)
        return self.plan(src, new_playlist, SyncDirection.MIRROR_A_TO_B, progress=progress, cancel=cancel)


def _group_actions(
    actions: list[SyncAction],
) -> dict[tuple[Service, str, str], list[SyncAction]]:
    """Group actions by (target service, target playlist id, kind), preserving
    first-seen order so batches apply in roughly the same sequence the plan
    listed them.

    Keying on the playlist id (not just the service) matters: a TWO_WAY sync
    between two playlists on the *same* service produces actions that share
    ``target`` but must land on two different playlists. Keying on service
    alone collapses them onto whichever playlist happened to be looked up
    last.
    """
    groups: dict[tuple[Service, str, str], list[SyncAction]] = {}
    for action in actions:
        key = (action.target, action.target_playlist_id, action.kind)
        groups.setdefault(key, []).append(action)
    return groups
