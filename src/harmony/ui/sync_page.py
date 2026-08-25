"""Pair up playlists, preview a sync plan, resolve ambiguous matches, apply."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from harmony.models import Playlist, Service  # noqa: E402
from harmony.tasks import CancelToken, on_main, run_async  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.widgets import ProgressDialog, missing_layer_status_page, status_page  # noqa: E402

log = logging.getLogger(__name__)

_DIRECTIONS = [
    ("MIRROR_A_TO_B", "Mirror source → target"),
    ("MIRROR_B_TO_A", "Mirror target → source"),
    ("TWO_WAY", "Two-way (union, never deletes)"),
]

# Maps config.Settings.default_direction's on-disk spelling to a SyncDirection
# enum member name, so the page's initial toggle honours the user's default.
_SETTINGS_DIRECTION_TO_ENUM = {
    "mirror-a-to-b": "MIRROR_A_TO_B",
    "mirror-b-to-a": "MIRROR_B_TO_A",
    "two-way": "TWO_WAY",
}


class SyncPage(Gtk.Box):
    """Source/target playlist pickers, direction choice, preview, apply."""

    def __init__(self, state: AppState) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.state = state
        self._plan: object | None = None
        self._playlist_choices: list[Playlist] = []

        if self.state.sync_engine is None:
            self.append(missing_layer_status_page("sync"))
            self.state.connect("providers-changed", self._maybe_recover)
            return

        self.append(self._build_controls())
        self.plan_stack = Gtk.Stack(vexpand=True)
        self.plan_stack.add_named(
            status_page(icon_name="emblem-synchronizing-symbolic", title="Preview a sync",
                        description="Pick a source and target playlist, then Preview."),
            "empty",
        )
        self.plan_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                                 margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        plan_scroller = Gtk.ScrolledWindow(child=self.plan_box, vexpand=True)
        self.plan_stack.add_named(plan_scroller, "plan")
        self.append(self.plan_stack)

        self.state.connect("playlists-changed", lambda *_a: self._refresh_playlist_choices())
        self._refresh_playlist_choices()

    def _maybe_recover(self, *_args: object) -> None:
        """If sync becomes available after providers reload, rebuild this page in place."""
        if self.state.sync_engine is not None:
            self.state.toast("Sync is now available — reopen the Sync page.")

    # -- controls -------------------------------------------------------------

    def _build_controls(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8,
                       margin_bottom=8, margin_start=8, margin_end=8)
        row = Gtk.Box(spacing=8)
        self.source_dropdown = Gtk.DropDown(hexpand=True)
        self.target_dropdown = Gtk.DropDown(hexpand=True)
        row.append(Gtk.Label(label="Source:"))
        row.append(self.source_dropdown)
        row.append(Gtk.Label(label="Target:"))
        row.append(self.target_dropdown)
        box.append(row)

        direction_row = Gtk.Box(spacing=8)
        self.direction_toggle = Adw.ToggleGroup()
        for name, label in _DIRECTIONS:
            self.direction_toggle.add(Adw.Toggle(name=name, label=label))
        default_name = _SETTINGS_DIRECTION_TO_ENUM.get(self.state.settings.default_direction, "TWO_WAY")
        self.direction_toggle.set_active_name(default_name)
        direction_row.append(self.direction_toggle)
        box.append(direction_row)

        action_row = Gtk.Box(spacing=8)
        self.preview_button = Gtk.Button(label="Preview", css_classes=["suggested-action"])
        self.preview_button.connect("clicked", self._on_preview_clicked)
        self.apply_button = Gtk.Button(label="Apply", sensitive=False)
        self.apply_button.connect("clicked", self._on_apply_clicked)
        action_row.append(self.preview_button)
        action_row.append(self.apply_button)
        box.append(action_row)
        return box

    def _refresh_playlist_choices(self) -> None:
        by_service = self.state.all_playlists()
        choices: list[Playlist] = []
        for service in Service:
            choices.extend(by_service.get(service, []))
        self._playlist_choices = choices
        labels = [f"{p.title} ({p.service.label})" for p in choices]
        model = Gtk.StringList.new(labels or ["No playlists available"])
        self.source_dropdown.set_model(model)
        self.target_dropdown.set_model(model)

    def _selected_playlist(self, dropdown: Gtk.DropDown) -> Playlist | None:
        index = dropdown.get_selected()
        if 0 <= index < len(self._playlist_choices):
            return self._playlist_choices[index]
        return None

    # -- preview ----------------------------------------------------------------

    def _on_preview_clicked(self, _button: Gtk.Button) -> None:
        source = self._selected_playlist(self.source_dropdown)
        target = self._selected_playlist(self.target_dropdown)
        if source is None or target is None:
            self.state.toast("Pick a source and target playlist first.")
            return
        if source.id == target.id and source.service == target.service:
            self.state.toast("Source and target must be different playlists.")
            return

        from harmony.sync import SyncDirection

        direction = getattr(SyncDirection, self.direction_toggle.get_active_name() or "TWO_WAY")

        cancel = CancelToken()
        dialog = ProgressDialog(self.get_root(), "Building sync plan", cancel)

        def progress(fraction: float, message: str) -> None:
            on_main(dialog.update, fraction, message)

        def work():  # noqa: ANN202 - SyncPlan, engine already validated non-None above
            return self.state.sync_engine.plan(source, target, direction, progress=progress, cancel=cancel)

        def done(plan: object) -> None:
            dialog.close()
            self._plan = plan
            self._render_plan(plan)

        def error(exc: BaseException) -> None:
            dialog.close()
            self.state.toast(f"Couldn't build sync plan: {exc}")

        dialog.present()
        run_async(work, done, error)

    def _render_plan(self, plan: object) -> None:
        while child := self.plan_box.get_first_child():
            self.plan_box.remove(child)

        summary = plan.summary() if hasattr(plan, "summary") else ""
        self.plan_box.append(Gtk.Label(label=summary, xalign=0.0, css_classes=["heading"]))

        actions = list(getattr(plan, "actions", []))
        adds = [a for a in actions if a.kind == "add"]
        removes = [a for a in actions if a.kind == "remove"]
        unmatched = [a for a in actions if a.kind == "unmatched"]

        if adds:
            self.plan_box.append(self._build_group("To add", adds, self._add_row))
        if removes:
            self.plan_box.append(self._build_group("To remove", removes, self._remove_row))
        if unmatched:
            self.plan_box.append(self._build_group("Unmatched — needs a decision", unmatched, self._unmatched_row))

        self.apply_button.set_sensitive(bool(adds or removes))
        self.plan_stack.set_visible_child_name("plan")

    def _build_group(self, title: str, actions: list, row_builder) -> Gtk.Widget:  # noqa: ANN001
        group = Adw.PreferencesGroup(title=f"{title} ({len(actions)})")
        for action in actions:
            group.add(row_builder(action))
        return group

    def _add_row(self, action) -> Gtk.Widget:  # noqa: ANN001
        track = action.track
        return Adw.ActionRow(title=track.title, subtitle=f"{track.artist_name} · {track.service.label}")

    def _remove_row(self, action) -> Gtk.Widget:  # noqa: ANN001
        track = action.track
        return Adw.ActionRow(title=track.title, subtitle=f"{track.artist_name} · {track.service.label}")

    def _unmatched_row(self, action) -> Gtk.Widget:  # noqa: ANN001
        track = action.track
        row = Adw.ExpanderRow(title=track.title, subtitle=track.artist_name)
        skip_button = Gtk.Button(label="Skip", valign=Gtk.Align.CENTER)
        skip_button.connect("clicked", lambda *_a: self._skip_action(action, row))
        row.add_suffix(skip_button)

        candidates = list(getattr(action.match, "candidates", []) or [])
        if not candidates:
            row.add_row(Adw.ActionRow(title="No candidates found on the target service"))
        for candidate in candidates[:8]:
            reasons = ", ".join(getattr(candidate, "reasons", []))
            candidate_row = Adw.ActionRow(
                title=candidate.track.title,
                subtitle=f"{candidate.track.artist_name} · score {candidate.score:.2f}" + (f" · {reasons}" if reasons else ""),
            )
            use_button = Gtk.Button(label="Use this", valign=Gtk.Align.CENTER)
            use_button.connect("clicked", lambda _b, a=action, c=candidate, r=row: self._use_candidate(a, c, r))
            candidate_row.add_suffix(use_button)
            row.add_row(candidate_row)
        return row

    def _use_candidate(self, action, candidate, expander_row: Adw.ExpanderRow) -> None:  # noqa: ANN001
        """Resolve an unmatched action in place so Apply picks it up as an add."""
        action.kind = "add"
        action.track = candidate.track
        expander_row.set_title(f"✓ {candidate.track.title}")
        expander_row.set_subtitle(f"Resolved · {candidate.track.artist_name}")
        expander_row.set_expanded(False)
        expander_row.set_sensitive(False)
        self.apply_button.set_sensitive(True)

    def _skip_action(self, action, expander_row: Adw.ExpanderRow) -> None:  # noqa: ANN001
        expander_row.set_title(f"Skipped: {action.track.title}")
        expander_row.set_subtitle("Will not be synced")
        expander_row.set_sensitive(False)

    # -- apply --------------------------------------------------------------------

    def _on_apply_clicked(self, _button: Gtk.Button) -> None:
        plan = self._plan
        if plan is None:
            return
        cancel = CancelToken()
        dialog = ProgressDialog(self.get_root(), "Applying sync", cancel)

        def progress(fraction: float, message: str) -> None:
            on_main(dialog.update, fraction, message)

        def work():  # noqa: ANN202 - SyncReport
            return self.state.sync_engine.apply(
                plan, progress=progress, cancel=cancel,
                snapshot_before_sync=self.state.settings.snapshot_before_sync,
            )

        def done(report: object) -> None:
            dialog.close()
            self._show_report(report)
            self.state.all_playlists(refresh=True)
            self.apply_button.set_sensitive(False)
            self._plan = None

        def error(exc: BaseException) -> None:
            dialog.close()
            self.state.toast(f"Sync failed: {exc}")

        dialog.present()
        run_async(work, done, error)

    def _show_report(self, report: object) -> None:
        added = len(getattr(report, "added", []))
        removed = len(getattr(report, "removed", []))
        skipped = len(getattr(report, "skipped", []))
        failed = getattr(report, "failed", [])
        body_lines = [f"Added: {added}", f"Removed: {removed}", f"Skipped: {skipped}"]
        if failed:
            body_lines.append(f"Failed: {len(failed)}")
            body_lines.extend(f"  • {getattr(a, 'track', a).title if hasattr(getattr(a, 'track', a), 'title') else a}: {msg}" for a, msg in failed[:5])
        dialog = Adw.AlertDialog(heading="Sync complete", body="\n".join(body_lines))
        dialog.add_response("ok", "OK")
        dialog.present(self)
        self.state.toast(f"Sync complete: {added} added, {removed} removed")
