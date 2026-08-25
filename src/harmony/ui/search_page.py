"""Unified search across whichever services are configured."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from harmony.models import Album, Artist, Playlist, SearchResults, Service, Track  # noqa: E402
from harmony.tasks import run_async  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.widgets import (  # noqa: E402
    build_track_column_view,
    error_status_page,
    replace_tracks,
    selected_tracks,
    set_stack_status,
    status_page,
)

log = logging.getLogger(__name__)

_KINDS = ["tracks", "albums", "artists", "playlists"]
_KIND_LABELS = ["Tracks", "Albums", "Artists", "Playlists"]
_SEARCH_DEBOUNCE_MS = 400


class SearchPage(Gtk.Box):
    """Search entry + service/kind filters + results, with per-track actions."""

    def __init__(self, state: AppState) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.state = state
        self._debounce_id: int | None = None
        self._showing_similar_to: Track | None = None
        self._last_results: SearchResults | None = None

        self.append(self._build_controls())

        self.content_stack = Gtk.Stack(vexpand=True)
        self.content_stack.add_named(
            status_page(icon_name="system-search-symbolic", title="Search Harmony",
                        description="Find tracks, albums, artists, and playlists across your services."),
            "empty",
        )
        self.column_view, self.track_store, self.track_selection = build_track_column_view()
        self.track_selection.connect("selection-changed", lambda *_a: self._update_action_sensitivity())
        tracks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._back_bar = Gtk.Box(spacing=6, visible=False)
        back_button = Gtk.Button(label="Back to search results")
        back_button.connect("clicked", self._on_back_to_results)
        self._back_bar.append(back_button)
        self._similar_label = Gtk.Label(xalign=0.0)
        self._back_bar.append(self._similar_label)
        tracks_box.append(self._back_bar)
        scroller = Gtk.ScrolledWindow(child=self.column_view, vexpand=True)
        tracks_box.append(scroller)
        tracks_box.append(self._build_track_actions())
        self.content_stack.add_named(tracks_box, "tracks")

        self.other_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.other_list.add_css_class("boxed-list")
        other_scroller = Gtk.ScrolledWindow(child=self.other_list, vexpand=True)
        self.content_stack.add_named(other_scroller, "other")

        self.content_stack.set_visible_child_name("empty")
        self.append(self.content_stack)

    # -- layout ---------------------------------------------------------------

    def _build_controls(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=8,
                       margin_bottom=8, margin_start=8, margin_end=8)
        self.search_entry = Gtk.SearchEntry(hexpand=True, placeholder_text="Search…")
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", lambda *_a: self._run_search(immediate=True))
        box.append(self.search_entry)

        self.kind_dropdown = Gtk.DropDown.new_from_strings(_KIND_LABELS)
        self.kind_dropdown.connect("notify::selected", lambda *_a: self._run_search(immediate=True))
        box.append(self.kind_dropdown)

        self.service_toggle = Adw.ToggleGroup()
        self.service_toggle.add(Adw.Toggle(name="both", label="Both"))
        self.service_toggle.add(Adw.Toggle(name=Service.YTMUSIC.value, label="YouTube Music"))
        self.service_toggle.add(Adw.Toggle(name=Service.QOBUZ.value, label="Qobuz"))
        self.service_toggle.set_active_name("both")
        self.service_toggle.connect("notify::active-name", lambda *_a: self._run_search(immediate=True))
        box.append(self.service_toggle)
        return box

    def _build_track_actions(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=6, margin_start=8, margin_end=8, margin_bottom=8)
        self.add_button = Gtk.Button(label="Add to Playlist")
        self.add_button.connect("clicked", self._on_add_clicked)
        self.find_button = Gtk.Button(label="Find on Other Service")
        self.find_button.connect("clicked", self._on_find_other_clicked)
        self.similar_button = Gtk.Button(label="Show Similar")
        self.similar_button.connect("clicked", self._on_show_similar_clicked)
        for button in (self.add_button, self.find_button, self.similar_button):
            button.set_sensitive(False)
            bar.append(button)
        return bar

    def focus_search_entry(self) -> None:
        self.search_entry.grab_focus()

    # -- searching --------------------------------------------------------------

    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        if self._debounce_id is not None:
            GLib.source_remove(self._debounce_id)
            self._debounce_id = None
        self._debounce_id = GLib.timeout_add(_SEARCH_DEBOUNCE_MS, self._on_debounce_elapsed)

    def _on_debounce_elapsed(self) -> bool:
        self._debounce_id = None
        self._run_search()
        return False

    def _target_providers(self) -> dict[Service, object]:
        active = self.service_toggle.get_active_name() or "both"
        if active == "both":
            return dict(self.state.providers)
        try:
            service = Service(active)
        except ValueError:
            return {}
        provider = self.state.providers.get(service)
        return {service: provider} if provider else {}

    def _run_search(self, *, immediate: bool = False) -> None:
        query = self.search_entry.get_text().strip()
        self._back_bar.set_visible(False)
        self._showing_similar_to = None
        if not query:
            self.content_stack.set_visible_child_name("empty")
            return
        providers = self._target_providers()
        if not providers:
            set_stack_status(
                self.content_stack,
                "empty",
                status_page(icon_name="network-offline-symbolic", title="No services configured",
                            description="Add an account in Preferences to search."),
            )
            return
        kind = _KINDS[self.kind_dropdown.get_selected()]
        self.content_stack.set_visible_child_name("empty")

        def work() -> SearchResults:
            merged = SearchResults()
            for service, provider in providers.items():
                try:
                    result = provider.search(query, kinds=(kind,), limit=25)
                except Exception as exc:  # noqa: BLE001 - one flaky service must not sink the query
                    log.warning("Search failed on %s: %s", service, exc)
                    continue
                merged.tracks.extend(result.tracks)
                merged.albums.extend(result.albums)
                merged.artists.extend(result.artists)
                merged.playlists.extend(result.playlists)
            return merged

        run_async(work, self._on_search_done, self._on_search_error)

    def _on_search_done(self, results: SearchResults) -> None:
        self._last_results = results
        if results.is_empty():
            set_stack_status(
                self.content_stack,
                "empty",
                status_page(icon_name="edit-find-symbolic", title="No results",
                            description="Try a different search."),
            )
            return
        kind = _KINDS[self.kind_dropdown.get_selected()]
        if kind == "tracks":
            replace_tracks(self.track_store, results.tracks)
            self.content_stack.set_visible_child_name("tracks")
        else:
            self._show_other_kind(kind, results)

    def _on_search_error(self, exc: BaseException) -> None:
        set_stack_status(self.content_stack, "empty", error_status_page(exc, title="Search failed"))
        self.state.toast(f"Search failed: {exc}")

    def _show_other_kind(self, kind: str, results: SearchResults) -> None:
        while row := self.other_list.get_row_at_index(0):
            self.other_list.remove(row)
        items: list[Album | Artist | Playlist]
        items = {"albums": results.albums, "artists": results.artists, "playlists": results.playlists}[kind]
        for item in items:
            subtitle = item.service.label
            if isinstance(item, Album):
                subtitle = f"{item.artist_name} · {item.service.label}"
            row = Adw.ActionRow(title=item.title if hasattr(item, "title") else item.name, subtitle=subtitle)
            row.set_activatable(True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, it=item: self._drill_into(it))
            self.other_list.append(row)
        self.content_stack.set_visible_child_name("other")

    def _drill_into(self, item: Album | Artist | Playlist) -> None:
        """Row-activate on a non-track result: load its tracks into the track view."""
        provider = self.state.providers.get(item.service)
        if provider is None:
            self.state.toast(f"No provider configured for {item.service.label}")
            return

        def work() -> list[Track]:
            if isinstance(item, Album):
                return provider.get_album_tracks(item.id)
            if isinstance(item, Artist):
                return provider.get_artist_top_tracks(item.id)
            return provider.get_playlist_tracks(item.id)

        run_async(work, self._on_drill_done, lambda exc: self.state.toast(f"Couldn't load tracks: {exc}"))

    def _on_drill_done(self, tracks: list[Track]) -> None:
        replace_tracks(self.track_store, tracks)
        self.content_stack.set_visible_child_name("tracks")

    # -- track actions ------------------------------------------------------

    def _update_action_sensitivity(self) -> None:
        tracks = selected_tracks(self.track_selection)
        self.add_button.set_sensitive(bool(tracks))
        self.find_button.set_sensitive(len(tracks) == 1)
        self.similar_button.set_sensitive(len(tracks) == 1)

    def _on_add_clicked(self, button: Gtk.Button) -> None:
        tracks = selected_tracks(self.track_selection)
        if not tracks:
            return
        self._open_playlist_popover(button, tracks)

    def _open_playlist_popover(self, parent: Gtk.Widget, tracks: list[Track]) -> None:
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
                row = Adw.ActionRow(title=playlist.title, subtitle=f"{service.label} · {playlist.track_count or 0} tracks")
                row.set_activatable(True)

                def _pick(_row: Adw.ActionRow, p: Playlist = playlist, pop: Gtk.Popover = popover) -> None:
                    pop.popdown()
                    self._add_tracks_to_playlist(tracks, p)

                row.connect("activated", _pick)
                listbox.append(row)
                found = True
        if not found:
            listbox.append(Adw.ActionRow(title="No playlists yet", sensitive=False))
        scroller = Gtk.ScrolledWindow(child=listbox, max_content_height=320,
                                       propagate_natural_height=True, width_request=280)
        popover.set_child(scroller)
        popover.set_parent(parent)
        popover.connect("closed", lambda p: p.unparent())
        popover.popup()

    def _add_tracks_to_playlist(self, tracks: list[Track], playlist: Playlist) -> None:
        provider = self.state.providers.get(playlist.service)
        if provider is None:
            self.state.toast(f"No provider configured for {playlist.service.label}")
            return
        ids = [t.id for t in tracks]

        def work() -> None:
            provider.add_tracks(playlist.id, ids)

        def done(_result: None) -> None:
            self.state.toast(f"Added {len(ids)} track(s) to {playlist.title}")
            self.state.all_playlists(refresh=True)

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't add tracks: {exc}"))

    def _on_find_other_clicked(self, _button: Gtk.Button) -> None:
        tracks = selected_tracks(self.track_selection)
        if len(tracks) != 1:
            return
        source = tracks[0]
        other = Service.QOBUZ if source.service == Service.YTMUSIC else Service.YTMUSIC
        target_provider = self.state.providers.get(other)
        if target_provider is None:
            self.state.toast(f"{other.label} isn't configured")
            return

        def work():  # noqa: ANN202 - MatchResult, imported lazily below
            from harmony.matching import match_track

            return match_track(source, target_provider)

        run_async(work, self._on_match_done, lambda exc: self.state.toast(f"Matching failed: {exc}"))

    def _on_match_done(self, result: object) -> None:
        best = getattr(result, "best", None)
        confidence = getattr(result, "confidence", "none")
        if best is None:
            self.state.toast("No match found on the other service.")
            return
        track = best.track
        self.state.toast(f"{confidence.title()} match: {track.artist_name} — {track.title} ({best.score:.2f})")

    def _on_show_similar_clicked(self, _button: Gtk.Button) -> None:
        tracks = selected_tracks(self.track_selection)
        if len(tracks) != 1:
            return
        track = tracks[0]
        provider = self.state.providers.get(track.service)
        if provider is None:
            return

        def work() -> list[Track]:
            return provider.similar_tracks(track)

        def done(similar: list[Track]) -> None:
            self._showing_similar_to = track
            replace_tracks(self.track_store, similar)
            self._similar_label.set_label(f"Similar to “{track.title}” by {track.artist_name}")
            self._back_bar.set_visible(True)
            self.content_stack.set_visible_child_name("tracks")

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't find similar tracks: {exc}"))

    def _on_back_to_results(self, _button: Gtk.Button) -> None:
        self._back_bar.set_visible(False)
        self._showing_similar_to = None
        if self._last_results is not None:
            replace_tracks(self.track_store, self._last_results.tracks)
