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

# Confidences that are trustworthy enough to act on automatically: a fresh
# exact/high-confidence search match, or a link previously written because a
# person manually confirmed it (see ``SyncEngine._record_link``). "low" and
# "none" must never drive an add or suppress a remove on their own — that is
# precisely the data-loss bug this module guards against.
_CONFIDENT_ENOUGH = {"exact", "high", "manual"}

# Sync-level sub-chunking for apply()'s per-group provider calls. This is
# deliberately independent of whatever chunk size a provider uses internally
# for its own network requests (YT 100 / Qobuz 50) — it exists purely so a
# large group has more than one cancellation checkpoint and more than one
# progress tick, without regressing all the way back to one provider call per
# track. See ``SyncEngine.apply``.
_APPLY_CHUNK_SIZE = 20


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


@dataclass(slots=True)
class SyncPlan:
    source: Playlist
    target: Playlist
    actions: list[SyncAction]
    notes: list[str] = field(default_factory=list)

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
        user-resolved (``manual``) matches are applied without asking. A
        ``high`` fuzzy match is then surfaced for confirmation instead, which
        is the conservative choice for anyone who has been bitten by a
        confident-looking wrong match.
        """
        self.providers = providers
        self.db = db
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        self.auto_accept_high = auto_accept_high

    @property
    def _confident_enough(self) -> set[str]:
        if self.auto_accept_high:
            return _CONFIDENT_ENOUGH
        return _CONFIDENT_ENOUGH - {"high"}

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
            actions, note = self._plan_one_direction(
                a, provider_a, b, provider_b, allow_remove=True, progress=progress, cancel=cancel
            )
            return SyncPlan(source=a, target=b, actions=actions, notes=[note] if note else [])

        if direction is SyncDirection.MIRROR_B_TO_A:
            actions, note = self._plan_one_direction(
                b, provider_b, a, provider_a, allow_remove=True, progress=progress, cancel=cancel
            )
            return SyncPlan(source=b, target=a, actions=actions, notes=[note] if note else [])

        if direction is SyncDirection.TWO_WAY:
            progress_ab, progress_ba = _split_progress(progress)
            actions_ab, note_ab = self._plan_one_direction(
                a, provider_a, b, provider_b, allow_remove=False, progress=progress_ab, cancel=cancel
            )
            actions_ba, note_ba = self._plan_one_direction(
                b, provider_b, a, provider_a, allow_remove=False, progress=progress_ba, cancel=cancel
            )
            notes = [n for n in (note_ab, note_ba) if n]
            return SyncPlan(source=a, target=b, actions=[*actions_ab, *actions_ba], notes=notes)

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
    ) -> tuple[list[SyncAction], str | None]:
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
        # Dst tracks that are "accounted for" by some source track and must
        # not be treated as orphans by the removal loop below.
        matched_dst_ids: set[str] = set()
        has_unmatched = False
        for result in results:
            best = result.best
            # Already-present short-circuit: if the best candidate is a real
            # (non-"none") guess and it already sits in the target playlist,
            # there is nothing to add regardless of confidence, and no reason
            # to bother the user with an "unmatched" prompt either — doing so
            # let the sync UI's default "use top candidate" resolution issue
            # a literal duplicate add for a track that was already correctly
            # in the target (e.g. a "low"-confidence cross-service duration
            # mismatch on an otherwise-correct match, like "Creep" scoring
            # 0.850 because of a 22s master-length difference). A "none"
            # confidence best guess is excluded: with no real candidate found,
            # a coincidental id match isn't trustworthy evidence of anything.
            if best is not None and result.confidence != "none" and best.track.id in dst_ids:
                matched_dst_ids.add(best.track.id)
                continue
            if best is not None and result.confidence in self._confident_enough:
                matched_dst_ids.add(best.track.id)
                actions.append(
                    SyncAction(
                        kind="add",
                        target=dst_provider.service,
                        track=best.track,
                        match=result,
                        target_playlist_id=dst_playlist.id,
                    )
                )
            else:
                # Low-confidence or empty result: don't guess, let the UI ask.
                # Bug: a failed/low-confidence match here does NOT mean the
                # target has no counterpart — it means we couldn't find one.
                # A removal must only ever mean "this target track has no
                # counterpart in the source", which is unknowable while any
                # source track's match outcome is still undetermined. See the
                # removal guard below.
                has_unmatched = True
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
            if has_unmatched:
                # Safest correct behaviour: never delete a target track on the
                # basis of a source track whose match outcome we don't trust
                # yet. Suppress ALL removals for this direction rather than
                # guess which ones would have been safe, and say why.
                unmatched_count = sum(1 for a in actions if a.kind == "unmatched")
                note = (
                    f"removals to {dst_playlist.title!r} skipped: {unmatched_count} source "
                    "track(s) unmatched — resolve them, then re-preview, before deleting anything"
                )
            else:
                for track in dst_tracks:
                    # Never remove a target track that some source track's
                    # match — confident add, or the already-present
                    # short-circuit above — accounted for. (This loop only
                    # runs when ``has_unmatched`` is False, i.e. every result
                    # landed in one of those two cases or matched nothing at
                    # all, so ``matched_dst_ids`` alone is the complete set.)
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
        return actions, note

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

        actionable = [a for a in plan.actions if a.kind in ("add", "remove")]
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

            # Sub-chunk each group so cancellation is honoured and progress
            # moves in more than one jump. A single provider call per whole
            # group (potentially hundreds of tracks) used to mean at most one
            # cancel check and one progress tick per group — a 50-track plan
            # cancelled right after it started ran to completion because
            # nothing between "start" and "done" ever looked at the token.
            # This is independent of whatever chunk size the provider itself
            # uses for its network requests (YT 100 / Qobuz 50): it exists so
            # *this* loop has checkpoints, not to optimise request counts.
            for sub in _chunked(batch, _APPLY_CHUNK_SIZE):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                self._apply_batch(kind, service, playlist, sub, report, cancel=cancel)
                done_count += len(sub)
                if progress is not None:
                    progress(done_count / total, sub[-1].track.title)

        report.skipped.extend(a for a in plan.actions if a.kind == "unmatched")
        return report

    def _apply_batch(
        self,
        kind: str,
        service: Service,
        playlist: Playlist,
        batch: list[SyncAction],
        report: SyncReport,
        *,
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
            self._apply_batch_fallback(kind, provider, playlist, batch, report, exc, cancel=cancel)
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
        cancel: CancelToken | None = None,
    ) -> None:
        """Retry a failed batch without risking duplicate writes.

        A failed batched call may have partially landed (see
        ``_apply_batch``'s docstring), so before retrying anything we re-read
        the target playlist's *current* track ids — one extra fetch, only on
        this error path — and use it to retry only what genuinely still needs
        doing: for "add", skip ids that are already present (they landed as
        part of the failed batch, or were already there) instead of adding
        them a second time; for "remove", skip ids that are already gone.

        If the playlist can't be re-read, we have no way to tell what already
        landed, so retrying blind would risk exactly the duplication this
        exists to prevent. In that case every action in the batch is recorded
        as failed instead — a recorded failure the user can retry beats a
        silently corrupted playlist.
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

        for action in batch:
            if cancel is not None:
                cancel.raise_if_cancelled()
            already_present = action.track.id in current_ids
            if kind == "add" and already_present:
                # Already landed (from the partially-failed batch, or from
                # before) — count it without writing it again.
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
        not "exact" or "high" — not just "none" — even if the applied id
        happens to equal ``best``'s id. "low" is an entirely ordinary
        cross-service scoring outcome (e.g. a 22s master-length difference),
        and the sync UI's natural click is the top-ranked candidate, which
        *is* ``match.best``. If a "low" link were cached verbatim at its
        original confidence, ``matching._match_one`` would keep returning it
        as authoritative forever, "low" is not in ``_CONFIDENT_ENOUGH``, so
        the action would be "unmatched" again on every subsequent sync —
        which rule 1's removal guard then treats as permanently unmatched,
        suppressing all removals for that direction forever, on top of
        re-prompting the user for the same track every single time. Treating
        the user's acceptance (of any confidence below "high") as "manual"
        is what breaks that loop: once cached as "manual", it lands in the
        confident branch of ``_plan_one_direction`` next time, where the
        already-present check applies and no duplicate add is generated.
        """
        if action.match is None or action.match.best is None:
            return
        src = action.match.source
        if action.track.id != action.match.best.track.id or action.match.confidence not in (
            "exact",
            "high",
        ):
            confidence, score_value = "manual", 1.0
        else:
            confidence, score_value = action.match.confidence, action.match.best.score
        self.db.put_link(
            src.service, src.id, action.track.service, action.track.id, score_value, confidence
        )

    def _snapshot(self, playlist: Playlist) -> None:
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
