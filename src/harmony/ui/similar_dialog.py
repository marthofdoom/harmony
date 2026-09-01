"""Shared "Similar music" results view.

Opened from tracks, artists, albums, and playlists across Search and
Playlists with a single entry point, ``present_similar``: it takes a
``fetch`` closure that produces the recommender's ``Suggestion`` list (see
``harmony.enrich.recommender``) and handles the loading/empty/error states
and the results list itself, so callers only need to decide *what* to fetch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from harmony.models import Playlist, Service, Track  # noqa: E402
from harmony.tasks import run_async  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.widgets import (  # noqa: E402
    _format_suggestion_subtitle,
    attach_context_menu,
    error_status_page,
    loading_status_page,
    open_list_popover,
    set_stack_status,
    status_page,
)

log = logging.getLogger(__name__)


def present_similar(
    parent: Gtk.Widget,
    state: AppState,
    *,
    title: str,
    fetch: Callable[[], list],
) -> None:
    """Present a dialog of recommender ``Suggestion``s produced by ``fetch``.

    ``fetch`` is expected to do network I/O (recommender/provider calls) and
    always runs off the main loop via ``run_async``, so callers may freely
    call things like ``provider.get_album_tracks`` inside it.
    """
    dialog = _SimilarDialog(state, title)
    dialog.present(parent)
    dialog.load(fetch)


class _SimilarDialog(Adw.Dialog):
    """Loading spinner -> results list (or empty/error state) -> optional
    "Create Playlist from these" footer."""

    def __init__(self, state: AppState, title: str) -> None:
        super().__init__(title=title, content_width=480, content_height=560)
        self.state = state
        self._suggestions: list = []

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())

        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(loading_status_page("Finding similar music…"), "loading")

        self.results_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.results_list.add_css_class("boxed-list")
        results_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            margin_top=8, margin_bottom=8, margin_start=8, margin_end=8,
        )
        results_box.append(Gtk.ScrolledWindow(child=self.results_list, vexpand=True))
        self.stack.add_named(results_box, "results")
        self.stack.set_visible_child_name("loading")
        toolbar_view.set_content(self.stack)

        footer = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8,
                          halign=Gtk.Align.END)
        self.create_button = Gtk.Button(
            label="Create Playlist from These", css_classes=["suggested-action"], sensitive=False,
        )
        self.create_button.connect("clicked", self._on_create_clicked)
        footer.append(self.create_button)
        toolbar_view.add_bottom_bar(footer)

        self.set_child(toolbar_view)

    # -- fetching -------------------------------------------------------------

    def load(self, fetch: Callable[[], list]) -> None:
        run_async(fetch, self._on_fetch_done, self._on_fetch_error)

    def _on_fetch_done(self, suggestions: list) -> None:
        self._suggestions = suggestions
        while row := self.results_list.get_row_at_index(0):
            self.results_list.remove(row)
        resolved_any = any(getattr(s, "resolved", None) is not None for s in suggestions)
        self.create_button.set_sensitive(resolved_any)
        if not suggestions:
            set_stack_status(
                self.stack, "empty",
                status_page(icon_name="edit-find-symbolic", title="No similar tracks found",
                            description="Try a different seed, or check that recommendation "
                            "sources are configured in Preferences."),
            )
            return
        for suggestion in suggestions:
            self.results_list.append(self._build_row(suggestion))
        self.stack.set_visible_child_name("results")

    def _on_fetch_error(self, exc: BaseException) -> None:
        set_stack_status(self.stack, "error", error_status_page(exc, title="Couldn't find similar music"))
        self.state.toast(f"Couldn't find similar music: {exc}")

    # -- rendering --------------------------------------------------------------

    def _build_row(self, suggestion: object) -> Adw.ActionRow:
        resolved: Track | None = getattr(suggestion, "resolved", None)
        # Humanize source keys ("lastfm" -> "Last.fm"); "provider" renders as the
        # target service's own label, best-effort from the resolved track or the
        # first configured service.
        target_label = (
            resolved.service.label if resolved is not None
            else next((s.label for s in Service if s in self.state.providers), "")
        )
        subtitle = _format_suggestion_subtitle(
            suggestion.artist, getattr(suggestion, "sources", None) or [], target_label
        )
        row = Adw.ActionRow(title=suggestion.title, subtitle=subtitle)
        icon = "emblem-ok-symbolic" if resolved is not None else "dialog-question-symbolic"
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))

        if resolved is not None:
            play_button = Gtk.Button(icon_name="media-playback-start-symbolic",
                                      tooltip_text="Play on Device", valign=Gtk.Align.CENTER)
            play_button.connect("clicked", lambda _b, t=resolved: self._open_device_popover(play_button, t))
            add_button = Gtk.Button(icon_name="list-add-symbolic",
                                     tooltip_text="Add to Playlist…", valign=Gtk.Align.CENTER)
            add_button.connect("clicked", lambda _b, t=resolved: self._open_playlist_popover(add_button, t))
            row.add_suffix(play_button)
            row.add_suffix(add_button)

            def build_actions(t: Track = resolved) -> list[tuple[str, Callable[[], None]]]:
                return [
                    ("Add to Playlist…", lambda: self._open_playlist_popover(row, t)),
                    ("Play on Device", lambda: self._open_device_popover(row, t)),
                ]

            attach_context_menu(row, build_actions)
        return row

    # -- per-suggestion actions: playlist/device popovers ------------------------
    #
    # Small, self-contained duplicates of search_page's
    # `_open_playlist_popover`/`_open_device_popover` idiom (single-track
    # variants) rather than a shared import, per the task's "duplicate the
    # minimal logic" allowance -- this dialog only ever acts on one resolved
    # track at a time, so the shape is simpler than search_page's
    # multi-selection version.

    def _open_playlist_popover(self, parent: Gtk.Widget, track: Track) -> None:
        playlists_by_service = self.state.all_playlists()
        popover = Gtk.Popover()
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        found = False
        for service in Service:
            provider = self.state.providers.get(service)
            if provider is None:
                continue
            for playlist in playlists_by_service.get(service, []):
                row = Adw.ActionRow(title=playlist.title,
                                     subtitle=f"{service.label} · {playlist.track_count or 0} tracks")
                row.set_activatable(True)

                def _pick(_row: Adw.ActionRow, p: Playlist = playlist, pop: Gtk.Popover = popover) -> None:
                    pop.popdown()
                    self._add_track_to_playlist(track, p)

                row.connect("activated", _pick)
                listbox.append(row)
                found = True
        if not found:
            listbox.append(Adw.ActionRow(title="No playlists yet", sensitive=False))
        open_list_popover(popover, parent, listbox)

    def _add_track_to_playlist(self, track: Track, playlist: Playlist) -> None:
        provider = self.state.providers.get(playlist.service)
        if provider is None:
            self.state.toast(f"No provider configured for {playlist.service.label}")
            return
        track_id = track.id

        def work() -> None:
            provider.add_tracks(playlist.id, [track_id])

        def done(_result: None) -> None:
            self.state.toast(f"Added “{track.title}” to {playlist.title}")
            self.state.all_playlists(refresh=True)
            self.state.emit("playlist-tracks-changed", playlist)

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't add track: {exc}"))

    def _open_device_popover(self, parent: Gtk.Widget, track: Track) -> None:
        devices = self.state.playback_targets()
        popover = Gtk.Popover()
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        if not devices:
            listbox.append(Adw.ActionRow(title="No devices yet", subtitle="Add one on the Devices page",
                                          sensitive=False))
        for info in devices:
            is_local = info.kind == "local"
            row = Adw.ActionRow(title=info.name,
                                subtitle="In this app" if is_local else info.host)
            row.set_activatable(True)
            row.add_prefix(Gtk.Image.new_from_icon_name(
                "computer-symbolic" if is_local else "audio-speakers-symbolic"))

            def _pick(_row: Adw.ActionRow, host: str = info.host, name: str = info.name,
                      pop: Gtk.Popover = popover) -> None:
                pop.popdown()
                self._play_track_on_device(track, host, name)

            row.connect("activated", _pick)
            listbox.append(row)
        open_list_popover(popover, parent, listbox)

    def _play_track_on_device(self, track: Track, host: str, name: str) -> None:
        def work() -> None:
            self.state.play_track_on_device(track, host)

        def done(_result: None) -> None:
            self.state.toast(f"Playing “{track.title}” on {name}")

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't play on {name}: {exc}"))

    # -- footer: create playlist from resolved suggestions -----------------------

    def _on_create_clicked(self, _button: Gtk.Button) -> None:
        resolved = [s.resolved for s in self._suggestions if getattr(s, "resolved", None) is not None]
        if not resolved:
            self.state.toast("None of these suggestions resolved to a real track.")
            return
        services = [s for s in Service if s in self.state.providers]
        if not services:
            self.state.toast("No services configured to create a playlist on.")
            return
        target_service = resolved[0].service if resolved[0].service in self.state.providers else services[0]
        self._prompt_playlist_name(target_service, resolved)

    def _prompt_playlist_name(self, service: Service, tracks: list[Track]) -> None:
        dialog = Adw.AlertDialog(heading="New Playlist")
        entry = Adw.EntryRow(title="Title", text=(self.get_title() or "Similar Mix"))
        group = Adw.PreferencesGroup()
        group.add(entry)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")

        def on_response(_dlg: Adw.AlertDialog, response: str) -> None:
            if response != "create":
                return
            title = entry.get_text().strip() or "Similar Mix"
            self._create_and_fill(service, title, tracks)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _create_and_fill(self, service: Service, title: str, tracks: list[Track]) -> None:
        provider = self.state.providers[service]
        ids = [t.id for t in tracks]

        def work() -> Playlist:
            playlist = provider.create_playlist(title)
            provider.add_tracks(playlist.id, ids)
            return playlist

        def done(_playlist: Playlist) -> None:
            count = len(ids)
            self.state.toast(
                GLib.ngettext("Created “%s” with %d track", "Created “%s” with %d tracks", count)
                % (title, count)
            )
            self.state.all_playlists(refresh=True)

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't create playlist: {exc}"))
