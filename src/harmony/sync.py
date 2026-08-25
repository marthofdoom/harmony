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


@dataclass(slots=True)
class SyncPlan:
    source: Playlist
    target: Playlist
    actions: list[SyncAction]

    def summary(self) -> str:
        adds = sum(1 for a in self.actions if a.kind == "add")
        removes = sum(1 for a in self.actions if a.kind == "remove")
        unmatched = sum(1 for a in self.actions if a.kind == "unmatched")
        return f"{adds} to add, {removes} to remove, {unmatched} unmatched"


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
            actions = self._plan_one_direction(
                a, provider_a, b, provider_b, allow_remove=True, progress=progress, cancel=cancel
            )
            return SyncPlan(source=a, target=b, actions=actions)

        if direction is SyncDirection.MIRROR_B_TO_A:
            actions = self._plan_one_direction(
                b, provider_b, a, provider_a, allow_remove=True, progress=progress, cancel=cancel
            )
            return SyncPlan(source=b, target=a, actions=actions)

        if direction is SyncDirection.TWO_WAY:
            progress_ab, progress_ba = _split_progress(progress)
            actions_ab = self._plan_one_direction(
                a, provider_a, b, provider_b, allow_remove=False, progress=progress_ab, cancel=cancel
            )
            actions_ba = self._plan_one_direction(
                b, provider_b, a, provider_a, allow_remove=False, progress=progress_ba, cancel=cancel
            )
            return SyncPlan(source=a, target=b, actions=[*actions_ab, *actions_ba])

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
    ) -> list[SyncAction]:
        if cancel is not None:
            cancel.raise_if_cancelled()
        src_tracks = src_provider.get_playlist_tracks(src_playlist.id)
        dst_tracks = dst_provider.get_playlist_tracks(dst_playlist.id)
        dst_ids = {t.id for t in dst_tracks}

        results = match_tracks(
            src_tracks, dst_provider, progress=_wrap_progress(progress, cancel), db=self.db
        )

        actions: list[SyncAction] = []
        matched_dst_ids: set[str] = set()
        for result in results:
            best = result.best
            if best is not None and result.confidence in ("exact", "high"):
                matched_dst_ids.add(best.track.id)
                if best.track.id in dst_ids:
                    continue  # already present on the target: no action needed
                actions.append(
                    SyncAction(kind="add", target=dst_provider.service, track=best.track, match=result)
                )
            else:
                # Low-confidence or empty result: don't guess, let the UI ask.
                actions.append(
                    SyncAction(
                        kind="unmatched", target=dst_provider.service, track=result.source, match=result
                    )
                )

        if allow_remove:
            if cancel is not None:
                cancel.raise_if_cancelled()
            for track in dst_tracks:
                if track.id not in matched_dst_ids:
                    actions.append(
                        SyncAction(kind="remove", target=dst_provider.service, track=track, match=None)
                    )
        return actions

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

        playlists_by_service = {plan.source.service: plan.source, plan.target.service: plan.target}
        actionable = [a for a in plan.actions if a.kind in ("add", "remove")]
        total = len(actionable) or 1

        for i, action in enumerate(actionable):
            if cancel is not None:
                cancel.raise_if_cancelled()
            self._apply_one(action, playlists_by_service, report)
            if progress is not None:
                progress((i + 1) / total, action.track.title)

        report.skipped.extend(a for a in plan.actions if a.kind == "unmatched")
        return report

    def _apply_one(
        self,
        action: SyncAction,
        playlists_by_service: dict[Service, Playlist],
        report: SyncReport,
    ) -> None:
        """Perform one add/remove. Any failure is swallowed into ``report.failed``
        so a single flaky provider call cannot abort the rest of the sync."""
        try:
            playlist = playlists_by_service.get(action.target)
            if playlist is None:
                raise ProviderError(f"No playlist known for service {action.target}")
            provider = self._provider_for(action.target)
            if action.kind == "add":
                provider.add_tracks(playlist.id, [action.track.id])
                report.added.append(action.track)
                if action.match is not None and action.match.best is not None:
                    src = action.match.source
                    self.db.put_link(
                        src.service,
                        src.id,
                        action.track.service,
                        action.track.id,
                        action.match.best.score,
                        action.match.confidence,
                    )
            elif action.kind == "remove":
                provider.remove_tracks(playlist.id, [action.track.id])
                report.removed.append(action.track)
        except Exception as exc:  # noqa: BLE001 - isolate one bad action from the rest
            log.warning("Sync action %s(%s) failed: %s", action.kind, action.track.title, exc)
            report.failed.append((action, str(exc)))

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
