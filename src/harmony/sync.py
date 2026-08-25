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
from .matching import MatchResult, match_tracks
from .models import Playlist, Service, Track

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
    def __init__(self, providers: dict[Service, MusicProvider], db: Database) -> None:
        self.providers = providers
        self.db = db

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
            src_tracks, dst_provider, progress=_wrap_progress(progress, cancel), db=self.db
        )

        actions: list[SyncAction] = []
        # Tracks whose id is safe to skip when looking for removal candidates:
        # ``matched_dst_ids`` are dst tracks a source track matched with
        # enough confidence to act on; ``accounted_dst_ids`` is the wider set
        # of "some source track's best guess landed here", used only as a
        # belt-and-braces guard below — see the removal loop.
        matched_dst_ids: set[str] = set()
        accounted_dst_ids: set[str] = set()
        has_unmatched = False
        for result in results:
            best = result.best
            if best is not None:
                accounted_dst_ids.add(best.track.id)
            if best is not None and result.confidence in _CONFIDENT_ENOUGH:
                matched_dst_ids.add(best.track.id)
                if best.track.id in dst_ids:
                    continue  # already present on the target: no action needed
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
                    # Per-track guard, redundant with the branch above today
                    # but kept as defense in depth: never remove a target
                    # track that any source track's match — confident or
                    # not — pointed at.
                    if track.id in matched_dst_ids or track.id in accounted_dst_ids:
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
            else:
                self._apply_batch(kind, service, playlist, batch, report)
            done_count += len(batch)
            if progress is not None:
                progress(done_count / total, batch[-1].track.title)

        report.skipped.extend(a for a in plan.actions if a.kind == "unmatched")
        return report

    def _apply_batch(
        self,
        kind: str,
        service: Service,
        playlist: Playlist,
        batch: list[SyncAction],
        report: SyncReport,
    ) -> None:
        """Apply one (service, playlist, kind) group as a single provider call.

        Batching lets the provider's own chunking do its job instead of
        issuing one network round-trip (and, for providers like YouTube Music
        whose ``remove_tracks`` re-fetches the whole playlist, one full
        re-fetch) per track. If the batched call fails, fall back to calling
        each action individually so one bad id can't fail the whole group —
        each failure is still recorded precisely in ``report.failed``.
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
            for action in batch:
                self._apply_one(action, provider, playlist, report)
            return

        for action in batch:
            if kind == "add":
                report.added.append(action.track)
                self._record_link(action)
            else:
                report.removed.append(action.track)

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
        candidate's score to the track the user actually picked. The same
        applies if the ids happen to coincide but the original match
        confidence was "none": a link is never cached with confidence
        "none", since ``matching._match_one`` treats any cached link as
        authoritative and would otherwise short-circuit every future match
        to "no match", which rule 1's removal guard would then treat as
        unmatched forever.
        """
        if action.match is None or action.match.best is None:
            return
        src = action.match.source
        if action.track.id != action.match.best.track.id or action.match.confidence == "none":
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
