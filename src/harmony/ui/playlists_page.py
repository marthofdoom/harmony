"""Browse, edit, import/export, and clone playlists across services."""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from harmony.models import Playlist, Service, Track  # noqa: E402
from harmony.tasks import run_async  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.widgets import (  # noqa: E402
    ProgressDialog,
    build_track_column_view,
    confirm_dialog,
    error_status_page,
    missing_layer_status_page,
    replace_tracks,
    selected_tracks,
    status_page,
)

log = logging.getLogger(__name__)

_EXPORT_FORMATS = ["m3u", "csv", "json"]


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

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True,
                                shrink_start_child=False, shrink_end_child=False)
        self.paned.set_start_child(self._build_playlist_list())
        self.paned.set_end_child(self._build_track_pane())
        self.paned.set_position(320)
        self.append(self.paned)

        self.state.connect("playlists-changed", lambda *_a: self._refresh_playlist_list())
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
                wrapper = Gtk.ListBoxRow(child=row)
                wrapper.playlist = playlist  # type: ignore[attr-defined]
                self.playlist_list.append(wrapper)
                if selected_id is not None and playlist.id == selected_id and playlist.service == self._selected_playlist.service:  # type: ignore[union-attr]
                    reselect = wrapper
        if reselect is not None:
            self.playlist_list.select_row(reselect)

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
        self.column_view, self.track_store, self.track_selection = build_track_column_view()
        self.track_selection.connect("selection-changed", lambda *_a: self._update_toolbar_sensitivity())
        self.track_stack.add_named(Gtk.ScrolledWindow(child=self.column_view), "tracks")
        box.append(self.track_stack)
        return box

    def _build_track_toolbar(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        self.rename_button = Gtk.Button(label="Rename")
        self.rename_button.connect("clicked", self._on_rename_clicked)
        self.delete_button = Gtk.Button(label="Delete")
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self._on_delete_clicked)
        self.remove_tracks_button = Gtk.Button(label="Remove Selected")
        self.remove_tracks_button.connect("clicked", self._on_remove_tracks_clicked)
        self.export_button = Gtk.Button(label="Export")
        self.export_button.connect("clicked", self._on_export_clicked)
        self.import_button = Gtk.Button(label="Import")
        self.import_button.connect("clicked", self._on_import_clicked)
        self.clone_button = Gtk.Button(label="Clone to Other Service")
        self.clone_button.connect("clicked", self._on_clone_clicked)
        for button in (self.rename_button, self.delete_button, self.remove_tracks_button,
                       self.export_button, self.import_button, self.clone_button):
            button.set_sensitive(False)
            bar.append(button)
        return bar

    def _update_toolbar_sensitivity(self) -> None:
        has_playlist = self._selected_playlist is not None
        for button in (self.rename_button, self.delete_button, self.export_button,
                       self.import_button, self.clone_button):
            button.set_sensitive(has_playlist)
        has_selection = has_playlist and bool(selected_tracks(self.track_selection))
        self.remove_tracks_button.set_sensitive(has_selection)

    def _load_tracks(self, playlist: Playlist) -> None:
        provider = self.state.providers.get(playlist.service)
        if provider is None:
            self.track_stack.add_named(missing_layer_status_page("providers"), "empty")
            self.track_stack.set_visible_child_name("empty")
            return
        self.track_stack.set_visible_child_name("empty")

        def work() -> list[Track]:
            return provider.get_playlist_tracks(playlist.id)

        def done(tracks: list[Track]) -> None:
            if self._selected_playlist is not playlist:
                return
            replace_tracks(self.track_store, tracks)
            self.track_stack.set_visible_child_name("tracks")

        def error(exc: BaseException) -> None:
            self.track_stack.add_named(error_status_page(exc, title="Couldn't load tracks"), "empty")
            self.track_stack.set_visible_child_name("empty")

        run_async(work, done, error)

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
            self.state.toast(f"Removed {len(ids)} track(s)")
            self._load_tracks(playlist)
            self.state.all_playlists(refresh=True)

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't remove tracks: {exc}"))

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

        def work() -> None:
            from harmony import io_formats

            tracks = provider.get_playlist_tracks(playlist.id)
            exporter = getattr(io_formats, f"export_{suffix}", None)
            if exporter is None:
                raise ValueError(f"Unsupported export format: .{suffix}")
            exporter(path, playlist, tracks)

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

        def work() -> int:
            from harmony import io_formats

            importer = getattr(io_formats, f"import_{suffix}", None)
            if importer is None:
                raise ValueError(f"Unsupported import format: .{suffix}")
            tracks = importer(path)
            track_ids = [t.id for t in tracks if getattr(t, "id", "")]
            if track_ids:
                provider.add_tracks(playlist.id, track_ids)
            return len(track_ids)

        def done(count: int) -> None:
            self.state.toast(f"Imported {count} track(s)")
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
            self.state.toast(f"Cloned {added} track(s) to {other.label}")
            self.state.all_playlists(refresh=True)

        def error(exc: BaseException) -> None:
            progress_dialog.close()
            self.state.toast(f"Clone failed: {exc}")

        progress_dialog.present()
        run_async(work, done, error)
