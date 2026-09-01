"""Browse, edit, import/export, and clone playlists across services."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from harmony.models import Playlist, Service, Track  # noqa: E402
from harmony.tasks import run_async  # noqa: E402
from harmony.ui.collection_actions import (  # noqa: E402
    add_collection_to_playlist,
    play_collection_on_device,
    track_menu_actions,
)
from harmony.ui.similar_dialog import present_similar  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.widgets import (  # noqa: E402
    ProgressDialog,
    action_status_page,
    attach_context_menu,
    build_track_column_view,
    confirm_dialog,
    error_status_page,
    loading_status_page,
    open_preferences,
    replace_tracks,
    selected_tracks,
    set_stack_status,
    status_page,
)

log = logging.getLogger(__name__)

_SUPPORTED_FORMATS = ["m3u", "csv", "json", "txt"]


@dataclass
class _ImportOutcome:
    """Result of importing a file and resolving it against a live provider."""

    added: int
    """Tracks actually added to the playlist (resolved at high+ confidence and written)."""
    total_rows: int
    """Every row the file claimed to contain, parsed or not."""
    rows_skipped: int
    """Rows that failed to parse (malformed M3U/CSV/JSON entries)."""
    unmatched: int
    """Rows that parsed fine but found no candidate at all on the target service."""
    low_confidence: int
    """Rows that parsed fine and matched *something*, but too weakly to trust
    unattended — never auto-added, only reported."""
    warnings: list[str] = field(default_factory=list)


def _is_dialog_dismissal(exc: GLib.Error) -> bool:
    """True when a ``Gtk.FileDialog`` async op failed because the user backed out."""
    return exc.matches(Gtk.DialogError.quark(), Gtk.DialogError.CANCELLED) or exc.matches(
        Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED
    )


class PlaylistsPage(Gtk.Box):
    """Two-pane playlist browser: playlist list on the left, tracks on the right."""

    def __init__(self, state: AppState) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.state = state
        self._selected_playlist: Playlist | None = None
        # (service, id) -> the playing-indicator icon on that playlist's sidebar
        # row, so the collection currently feeding the Now Playing bar lights up.
        self._collection_indicators: dict[tuple[Service, str], Gtk.Widget] = {}

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True,
                                shrink_start_child=False, shrink_end_child=False)
        self.paned.set_start_child(self._build_playlist_list())
        self.paned.set_end_child(self._build_track_pane())
        self.paned.set_position(320)
        self.append(self.paned)

        self.state.connect("playlists-changed", lambda *_a: self._refresh_playlist_list())
        self.state.connect("playlist-tracks-changed", self._on_playlist_tracks_changed)
        self.state.connect("playback-changed", lambda *_a: self._refresh_playing_collection())
        self._refresh_playlist_list()

    # -- left pane: playlist list ------------------------------------------

    def _build_playlist_list(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toolbar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        new_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New playlist")
        new_button.connect("clicked", self._on_new_clicked)
        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh_button.connect("clicked", lambda *_a: self.state.all_playlists(refresh=True))
        toolbar.append(new_button)
        toolbar.append(refresh_button)
        box.append(toolbar)

        self.playlist_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.playlist_list.add_css_class("navigation-sidebar")
        self.playlist_list.connect("row-selected", self._on_playlist_selected)
        scroller = Gtk.ScrolledWindow(child=self.playlist_list, vexpand=True)
        box.append(scroller)
        return box

    def _refresh_playlist_list(self) -> None:
        by_service = self.state.all_playlists()
        selected_id = self._selected_playlist.id if self._selected_playlist else None
        while row := self.playlist_list.get_row_at_index(0):
            self.playlist_list.remove(row)
        self._collection_indicators.clear()
        reselect: Gtk.ListBoxRow | None = None
        for service in Service:
            playlists = by_service.get(service, [])
            if not playlists:
                continue
            header = Gtk.ListBoxRow(selectable=False, activatable=False)
            label = Gtk.Label(label=service.label, xalign=0.0, margin_top=10, margin_start=8)
            label.add_css_class("heading")
            header.set_child(label)
            self.playlist_list.append(header)
            for playlist in playlists:
                row = Adw.ActionRow(title=playlist.title, subtitle=f"{playlist.track_count or 0} tracks")
                row.playlist = playlist  # type: ignore[attr-defined]
                indicator = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
                indicator.add_css_class("accent")
                indicator.set_visible(False)
                row.add_prefix(indicator)
                self._collection_indicators[(playlist.service, playlist.id)] = indicator
                wrapper = Gtk.ListBoxRow(child=row)
                wrapper.playlist = playlist  # type: ignore[attr-defined]
                attach_context_menu(row, lambda p=playlist, w=wrapper, a=row: self._playlist_row_actions(p, w, a))
                self.playlist_list.append(wrapper)
                if selected_id is not None and playlist.id == selected_id and playlist.service == self._selected_playlist.service:  # type: ignore[union-attr]
                    reselect = wrapper
        if reselect is not None:
            self.playlist_list.select_row(reselect)
        self._refresh_playing_collection()

    def _refresh_playing_collection(self) -> None:
        """Light up the sidebar row whose playlist is feeding playback."""
        active = self.state.playback.collection_key
        for key, indicator in self._collection_indicators.items():
            indicator.set_visible(key == active)

    def _playlist_row_actions(
        self, playlist: Playlist, wrapper: Gtk.ListBoxRow, anchor: Gtk.Widget | None = None
    ) -> list[tuple[str, Callable[[], None]]]:
        # ``anchor`` is the right-clicked row; the pickers parent to it (not the
        # page-sized sidebar list) so they open under the pointer, in bounds.
        anchor = anchor or self.playlist_list
        actions: list[tuple[str, Callable[[], None]]] = []
        # get_playlist_tracks needs the playlist's own native provider -- no
        # fallback to another service makes sense here.
        provider = self.state.providers.get(playlist.service)
        if provider is not None:

            def fetch_tracks(p: object = provider) -> list[Track]:
                return p.get_playlist_tracks(playlist.id)

            actions.append((
                "Play on Device",
                lambda: play_collection_on_device(
                    anchor, self.state, label=playlist.title, fetch_tracks=fetch_tracks,
                    collection_key=(playlist.service, playlist.id),
                ),
            ))
            actions.append((
                "Add to Playlist…",
                lambda: add_collection_to_playlist(
                    anchor, self.state, label=playlist.title, fetch_tracks=fetch_tracks
                ),
            ))
            actions.append(("Show Similar", lambda: self._show_similar_for_playlist(playlist, provider)))
        actions.append(("Open", lambda: self.playlist_list.select_row(wrapper)))
        return actions

    def _show_similar_for_playlist(self, playlist: Playlist, provider: object) -> None:
        if self.state.recommender is None:
            self.state.toast("Recommendations aren't available.")
            return
        present_similar(
            self,
            self.state,
            title=f"Similar to {playlist.title}",
            fetch=lambda: self.state.recommender.expand_playlist(
                provider.get_playlist_tracks(playlist.id), provider, limit=40
            ),
        )

    def _on_playlist_tracks_changed(self, _state: AppState, playlist: Playlist) -> None:
        """Reload the tracks pane when the open playlist gains tracks elsewhere."""
        selected = self._selected_playlist
        if (
            selected is not None
            and playlist is not None
            and selected.id == playlist.id
            and selected.service == playlist.service
        ):
            self._load_tracks(selected)

    def _on_playlist_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        playlist = getattr(row, "playlist", None) if row is not None else None
        self._selected_playlist = playlist
        self._update_toolbar_sensitivity()
        if playlist is None:
            self.track_stack.set_visible_child_name("empty")
            return
        self._load_tracks(playlist)

    # -- right pane: tracks --------------------------------------------------

    def _build_track_pane(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toolbar = self._build_track_toolbar()
        box.append(self.toolbar)

        self.track_stack = Gtk.Stack(vexpand=True)
        self.track_stack.add_named(
            status_page(icon_name="view-list-symbolic", title="No playlist selected",
                        description="Choose a playlist on the left to see its tracks."),
            "empty",
        )
        self.column_view, self.track_store, self.track_selection = build_track_column_view(
            on_row_menu=self._track_row_actions, state=self.state
        )
        self.track_selection.connect("selection-changed", lambda *_a: self._update_toolbar_sensitivity())
        self.track_stack.add_named(Gtk.ScrolledWindow(child=self.column_view), "tracks")
        self.track_stack.add_named(loading_status_page("Loading tracks…"), "loading")
        box.append(self.track_stack)
        return box

    def _build_track_toolbar(self) -> Gtk.Widget:
        bar = Gtk.ActionBar()

        # "Remove Selected" is the one action tied to the track selection, so it
        # stays out front; the playlist-level actions collapse into an overflow
        # menu instead of six always-visible buttons.
        self.remove_tracks_button = Gtk.Button(label="Remove Selected")
        self.remove_tracks_button.connect("clicked", self._on_remove_tracks_clicked)
        bar.pack_start(self.remove_tracks_button)

        menu = Gio.Menu()
        menu.append("Rename…", "playlist.rename")
        menu.append("Export…", "playlist.export")
        menu.append("Import…", "playlist.import")
        menu.append("Clone to Other Service", "playlist.clone")
        menu.append("Delete…", "playlist.delete")

        actions = Gio.SimpleActionGroup()
        for name, handler in (
            ("rename", self._on_rename_clicked),
            ("export", self._on_export_clicked),
            ("import", self._on_import_clicked),
            ("clone", self._on_clone_clicked),
            ("delete", self._on_delete_clicked),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h(None))
            actions.add_action(action)
        self._playlist_actions = actions
        self.insert_action_group("playlist", actions)

        self.playlist_menu_button = Gtk.MenuButton(
            icon_name="view-more-symbolic", menu_model=menu, tooltip_text="Playlist actions",
        )
        bar.pack_end(self.playlist_menu_button)

        self._update_toolbar_sensitivity()
        return bar

    def _update_toolbar_sensitivity(self) -> None:
        has_playlist = self._selected_playlist is not None
        # The overflow menu's items are all playlist-level; gate the whole menu.
        for name in ("rename", "export", "import", "clone", "delete"):
            action = self._playlist_actions.lookup_action(name)
            if action is not None:
                action.set_enabled(has_playlist)
        self.playlist_menu_button.set_sensitive(has_playlist)
        has_selection = has_playlist and bool(selected_tracks(self.track_selection))
        self.remove_tracks_button.set_sensitive(has_selection)

    def _load_tracks(self, playlist: Playlist) -> None:
        provider = self.state.providers.get(playlist.service)
        if provider is None:
            set_stack_status(
                self.track_stack, "empty",
                action_status_page(
                    icon_name="network-offline-symbolic",
                    title=f"{playlist.service.label} isn't connected",
                    description=f"Connect {playlist.service.label} in Preferences to see this "
                    "playlist's tracks.",
                    action_label="Open Preferences",
                    on_action=lambda: open_preferences(self, "accounts"),
                ),
            )
            return
        self.track_stack.set_visible_child_name("loading")

        def work() -> list[Track]:
            return provider.get_playlist_tracks(playlist.id)

        def done(tracks: list[Track]) -> None:
            if self._selected_playlist is not playlist:
                return
            replace_tracks(self.track_store, tracks)
            self.track_stack.set_visible_child_name("tracks")

        def error(exc: BaseException) -> None:
            if self._selected_playlist is not playlist:
                return
            set_stack_status(self.track_stack, "empty", error_status_page(exc, title="Couldn't load tracks"))

        run_async(work, done, error)

    def _track_row_actions(self, track: Track) -> list[tuple[str, Callable[[], None]]]:
        """Right-click menu for a track row: same shape as Search's own track list."""
        return track_menu_actions(self.column_view, self.state, track)

    # -- toolbar actions ------------------------------------------------------

    def _on_new_clicked(self, _button: Gtk.Button) -> None:
        services = [s for s in Service if s in self.state.providers]
        if not services:
            self.state.toast("Set up an account in Preferences first.")
            return
        dialog = Adw.AlertDialog(heading="New Playlist", body="")
        entry = Adw.EntryRow(title="Title")
        service_row = Adw.ComboRow(title="Service", model=Gtk.StringList.new([s.label for s in services]))
        group = Adw.PreferencesGroup()
        group.add(entry)
        group.add(service_row)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")

        def on_response(_dlg: Adw.AlertDialog, response: str) -> None:
            if response != "create":
                return
            title = entry.get_text().strip()
            if not title:
                self.state.toast("Playlist needs a title.")
                return
            service = services[service_row.get_selected()]
            self._create_playlist(service, title)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _create_playlist(self, service: Service, title: str) -> None:
        provider = self.state.providers[service]

        def work() -> Playlist:
            return provider.create_playlist(title)

        def done(_playlist: Playlist) -> None:
            self.state.toast(f"Created “{title}” on {service.label}")
            self.state.all_playlists(refresh=True)

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't create playlist: {exc}"))

    def _on_rename_clicked(self, _button: Gtk.Button) -> None:
        playlist = self._selected_playlist
        if playlist is None:
            return
        dialog = Adw.AlertDialog(heading="Rename Playlist")
        entry = Adw.EntryRow(title="Title", text=playlist.title)
        group = Adw.PreferencesGroup()
        group.add(entry)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")

        def on_response(_dlg: Adw.AlertDialog, response: str) -> None:
            if response != "rename":
                return
            title = entry.get_text().strip()
            if not title:
                return
            provider = self.state.providers.get(playlist.service)
            if provider is None:
                return

            def work() -> None:
                provider.rename_playlist(playlist.id, title)

            def done(_r: None) -> None:
                self.state.toast("Playlist renamed")
                self.state.all_playlists(refresh=True)

            run_async(work, done, lambda exc: self.state.toast(f"Rename failed: {exc}"))

        dialog.connect("response", on_response)
        dialog.present(self)

    def _on_delete_clicked(self, _button: Gtk.Button) -> None:
        playlist = self._selected_playlist
        if playlist is None:
            return

        def confirmed() -> None:
            provider = self.state.providers.get(playlist.service)
            if provider is None:
                return

            def work() -> None:
                provider.delete_playlist(playlist.id)

            def done(_r: None) -> None:
                self.state.toast(f"Deleted “{playlist.title}”")
                self._selected_playlist = None
                self.state.all_playlists(refresh=True)

            run_async(work, done, lambda exc: self.state.toast(f"Delete failed: {exc}"))

        confirm_dialog(
            self, "Delete Playlist?", f"“{playlist.title}” will be permanently deleted from {playlist.service.label}.",
            on_confirm=confirmed,
        )

    def _on_remove_tracks_clicked(self, _button: Gtk.Button) -> None:
        playlist = self._selected_playlist
        if playlist is None:
            return
        tracks = selected_tracks(self.track_selection)
        if not tracks:
            return
        provider = self.state.providers.get(playlist.service)
        if provider is None:
            return
        ids = [t.id for t in tracks]

        def work() -> None:
            provider.remove_tracks(playlist.id, ids)

        def done(_r: None) -> None:
            count = len(ids)
            self.state.toast(GLib.ngettext("Removed %d track", "Removed %d tracks", count) % count)
            self._load_tracks(playlist)
            self.state.all_playlists(refresh=True)

        run_async(work, done, lambda exc: self.state.toast("Couldn't remove those tracks — check your connection."))

    def _on_export_clicked(self, _button: Gtk.Button) -> None:
        playlist = self._selected_playlist
        if playlist is None:
            return
        dialog = Gtk.FileDialog(initial_name=f"{playlist.title}.m3u")
        dialog.save(self.get_root(), None, self._on_export_path_chosen, playlist)

    def _on_export_path_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult, playlist: Playlist) -> None:
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error as exc:
            if not _is_dialog_dismissal(exc):
                self.state.toast(f"Export failed: {exc.message}")
            return
        path = Path(gfile.get_path())
        provider = self.state.providers.get(playlist.service)
        if provider is None:
            return
        suffix = path.suffix.lstrip(".").lower() or "m3u"
        if suffix not in _SUPPORTED_FORMATS:
            self.state.toast(f"Unsupported export format: .{suffix}")
            return

        def work() -> None:
            from harmony import io_formats

            tracks = provider.get_playlist_tracks(playlist.id)
            # The three exporters don't share a signature (export_json also
            # needs the playlist for its metadata block), so dispatch by hand
            # rather than pretend they're interchangeable via getattr.
            if suffix == "m3u":
                io_formats.export_m3u(tracks, path)
            elif suffix == "csv":
                io_formats.export_csv(tracks, path)
            elif suffix == "txt":
                io_formats.export_txt(tracks, path)
            else:
                io_formats.export_json(playlist, tracks, path)

        def done(_r: None) -> None:
            self.state.toast(f"Exported to {path.name}")

        run_async(work, done, lambda exc: self.state.toast(f"Export failed: {exc}"))

    def _on_import_clicked(self, _button: Gtk.Button) -> None:
        playlist = self._selected_playlist
        if playlist is None:
            return
        dialog = Gtk.FileDialog()
        dialog.open(self.get_root(), None, self._on_import_path_chosen, playlist)

    def _on_import_path_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult, playlist: Playlist) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error as exc:
            if not _is_dialog_dismissal(exc):
                self.state.toast(f"Import failed: {exc.message}")
            return
        path = Path(gfile.get_path())
        provider = self.state.providers.get(playlist.service)
        if provider is None:
            return
        suffix = path.suffix.lstrip(".").lower()
        if suffix not in _SUPPORTED_FORMATS:
            self.state.toast(f"Unsupported import format: .{suffix}")
            return

        def work() -> _ImportOutcome:
            from harmony import io_formats

            importer = {"m3u": io_formats.import_m3u, "csv": io_formats.import_csv,
                        "json": io_formats.import_json, "txt": io_formats.import_txt}[suffix]
            # Importers only ever hand back loose descriptor dicts (no stable
            # cross-service id survives an M3U/CSV/JSON round trip) — they must
            # be re-resolved against the target provider's real catalog via
            # matching, same as a hand-typed playlist would be. That's a
            # network operation, so it has to stay on this worker thread.
            descriptors, parse_warnings = importer(path)
            match_results = io_formats.resolve_imported(descriptors, provider)
            # ``resolve_imported`` deliberately returns full MatchResults (not
            # just the winning Track) so this UI can decide what's confident
            # enough to add automatically instead of blindly trusting `best`
            # -- `best` is just the top candidate, however weak, and e.g. a
            # "none"-confidence guess (a completely unrelated track that
            # merely scored highest among bad options) must never be
            # silently added just because *something* was returned.
            _AUTO_ADD_CONFIDENCE = {"exact", "high", "manual"}
            confident = [r for r in match_results if r.best is not None and r.confidence in _AUTO_ADD_CONFIDENCE]
            no_match = [r for r in match_results if r.best is None]
            low_confidence = [
                r for r in match_results
                if r.best is not None and r.confidence not in _AUTO_ADD_CONFIDENCE
            ]
            resolved_ids = [r.best.track.id for r in confident]
            if resolved_ids:
                provider.add_tracks(playlist.id, resolved_ids)

            unmatched_warnings = [
                f"could not match {r.source.title!r} ({', '.join(r.source.artists) or 'unknown artist'})"
                for r in no_match
            ]
            low_confidence_warnings = [
                f"low-confidence match skipped: {r.source.title!r} "
                f"({', '.join(r.source.artists) or 'unknown artist'}) -> "
                f"{r.best.track.title!r} ({r.confidence}, score {r.best.score:.2f})"
                for r in low_confidence
            ]

            # ``parse_warnings`` isn't uniformly "one warning per unparsed
            # row": the M3U/CSV/JSON per-item loops do emit exactly that
            # ("line N: ...", "row N: ...", "item N: ..."), but import_json
            # can also fail on the *whole payload* before any row-by-row walk
            # even starts (unreadable file, wrong top-level shape) and emit a
            # single warning that doesn't correspond to any one row. Treating
            # that as "+1 unparsed row" would misstate the denominator, so
            # only count warnings that are actually attributable to a row.
            row_warnings = [w for w in parse_warnings if w.startswith(("line ", "row ", "item "))]
            payload_warnings = [w for w in parse_warnings if w not in row_warnings]

            return _ImportOutcome(
                added=len(resolved_ids),
                total_rows=len(descriptors) + len(row_warnings),
                rows_skipped=len(row_warnings),
                unmatched=len(no_match),
                low_confidence=len(low_confidence),
                warnings=[*payload_warnings, *row_warnings, *unmatched_warnings, *low_confidence_warnings],
            )

        def done(outcome: _ImportOutcome) -> None:
            for warning in outcome.warnings:
                log.warning("Import %s: %s", path.name, warning)
            message = GLib.ngettext(
                "Imported %d of %d track", "Imported %d of %d tracks", outcome.total_rows
            ) % (outcome.added, outcome.total_rows)
            detail = []
            if outcome.rows_skipped:
                detail.append(GLib.ngettext(
                    "%d row skipped", "%d rows skipped", outcome.rows_skipped) % outcome.rows_skipped)
            if outcome.unmatched:
                detail.append(f"{outcome.unmatched} unmatched")
            if outcome.low_confidence:
                detail.append(GLib.ngettext(
                    "%d low-confidence match skipped", "%d low-confidence matches skipped",
                    outcome.low_confidence) % outcome.low_confidence)
            if detail:
                message += " (" + ", ".join(detail) + ")"
            self.state.toast(message)
            self._load_tracks(playlist)

        run_async(work, done, lambda exc: self.state.toast(f"Import failed: {exc}"))

    def _on_clone_clicked(self, _button: Gtk.Button) -> None:
        playlist = self._selected_playlist
        if playlist is None:
            return
        other = Service.QOBUZ if playlist.service == Service.YTMUSIC else Service.YTMUSIC
        if other not in self.state.providers:
            self.state.toast(f"{other.label} isn't configured")
            return
        if self.state.sync_engine is None:
            self.state.toast("Sync isn't available yet.")
            return

        from harmony.tasks import CancelToken

        cancel = CancelToken()
        progress_dialog = ProgressDialog(self.get_root(), f"Cloning to {other.label}", cancel)

        def on_progress(fraction: float, message: str) -> None:
            from harmony.tasks import on_main

            on_main(progress_dialog.update, fraction, message)

        def work():  # noqa: ANN202 - SyncReport, engine imported lazily upstream
            engine = self.state.sync_engine
            plan = engine.clone_playlist(playlist, other, progress=on_progress, cancel=cancel)
            return engine.apply(plan, progress=on_progress, cancel=cancel)

        def done(report: object) -> None:
            progress_dialog.close()
            added = len(getattr(report, "added", []))
            self.state.toast(
                GLib.ngettext("Cloned %d track to %s", "Cloned %d tracks to %s", added)
                % (added, other.label)
            )
            self.state.all_playlists(refresh=True)

        def error(exc: BaseException) -> None:
            progress_dialog.close()
            self.state.toast(f"Clone failed: {exc}")

        progress_dialog.present()
        # Cancelled runs invoke neither `done` nor `error` (that's the whole
        # point of cancellation being silent) — without on_cancelled the
        # modal, undismissable ProgressDialog would just sit there forever.
        run_async(work, done, error, on_cancelled=progress_dialog.close)
