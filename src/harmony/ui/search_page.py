"""Unified search across whichever services are configured."""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from harmony.errors import NotSupportedError  # noqa: E402
from harmony.models import Album, Artist, Playlist, SearchResults, Service, Track  # noqa: E402
from harmony.tasks import run_async  # noqa: E402
from harmony.ui.collection_actions import (  # noqa: E402
    add_collection_to_playlist,
    play_collection_on_device,
)
from harmony.ui.similar_dialog import present_similar  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.widgets import (  # noqa: E402
    attach_context_menu,
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
        # Artist drill-down state (search result -> artist -> albums, with an
        # explicit "Most popular"/"Top songs" side trip into tracks). See
        # ``_drill_into``/``_show_artist_albums``/`_on_show_artist_top_tracks_clicked``.
        self._showing_artist: Artist | None = None
        self._showing_artist_top_tracks_for: Artist | None = None

        self.append(self._build_controls())

        self.content_stack = Gtk.Stack(vexpand=True)
        self.content_stack.add_named(
            status_page(icon_name="system-search-symbolic", title="Search Harmony",
                        description="Find tracks, albums, artists, and playlists across your services."),
            "empty",
        )
        self.column_view, self.track_store, self.track_selection = build_track_column_view(
            on_row_menu=self._track_row_actions, state=self.state
        )
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
        other_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._other_back_bar = Gtk.Box(spacing=6, visible=False)
        other_back_button = Gtk.Button(label="Back to search results")
        other_back_button.connect("clicked", self._on_other_back_clicked)
        self._other_back_bar.append(other_back_button)
        self._other_header_label = Gtk.Label(xalign=0.0, hexpand=True)
        self._other_back_bar.append(self._other_header_label)
        self._other_popular_button = Gtk.Button(label="Most popular", visible=False)
        self._other_popular_button.connect("clicked", self._on_show_artist_top_tracks_clicked)
        self._other_back_bar.append(self._other_popular_button)
        other_box.append(self._other_back_bar)
        other_scroller = Gtk.ScrolledWindow(child=self.other_list, vexpand=True)
        other_box.append(other_scroller)
        self.content_stack.add_named(other_box, "other")

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
        self.play_button = Gtk.Button(label="Play on Device")
        self.play_button.connect("clicked", self._on_play_device_clicked)
        for button in (self.add_button, self.find_button, self.similar_button, self.play_button):
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
        self._showing_artist_top_tracks_for = None
        self._other_back_bar.set_visible(False)
        self._other_popular_button.set_visible(False)
        self._showing_artist = None
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
        # A fresh set of search results, not an artist drill-down -- clear
        # any artist context so its header/"Most popular" button don't leak
        # into a plain albums/artists/playlists listing.
        self._other_back_bar.set_visible(False)
        self._other_popular_button.set_visible(False)
        self._showing_artist = None
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
            attach_context_menu(row, lambda it=item: self._other_row_actions(it))
            self.other_list.append(row)
        self.content_stack.set_visible_child_name("other")

    def _drill_into(self, item: Album | Artist | Playlist) -> None:
        """Row-activate on a non-track result.

        An ``Artist`` opens that artist's ALBUMS (not tracks) -- "most
        popular"/"top songs" is a separate, explicit action from there (see
        ``_show_artist_albums``). Albums/playlists still load straight into
        the track view, same as before.
        """
        provider = self.state.providers.get(item.service)
        if provider is None:
            self.state.toast(f"No provider configured for {item.service.label}")
            return

        if isinstance(item, Artist):
            self._show_artist_albums(item)
            return

        def work() -> list[Track]:
            if isinstance(item, Album):
                return provider.get_album_tracks(item.id)
            return provider.get_playlist_tracks(item.id)

        run_async(work, self._on_drill_done, lambda exc: self.state.toast(f"Couldn't load tracks: {exc}"))

    def _on_drill_done(self, tracks: list[Track]) -> None:
        # Plain album/playlist track drill-down carries no "similar" or
        # "artist top tracks" context; clear both so a stale back bar from an
        # earlier view can't linger into this one.
        self._back_bar.set_visible(False)
        self._showing_similar_to = None
        self._showing_artist_top_tracks_for = None
        replace_tracks(self.track_store, tracks)
        self.content_stack.set_visible_child_name("tracks")

    # -- artist drill-down: artist -> albums, with an explicit "most popular" ----

    def _show_artist_albums(self, artist: Artist) -> None:
        provider = self.state.providers.get(artist.service)
        if provider is None:
            self.state.toast(f"No provider configured for {artist.service.label}")
            return

        def work() -> list[Album]:
            return provider.get_artist_albums(artist.id)

        def done(albums: list[Album]) -> None:
            self._showing_artist = artist
            while row := self.other_list.get_row_at_index(0):
                self.other_list.remove(row)
            if albums:
                for album in albums:
                    subtitle = f"{album.artist_name} · {album.service.label}" if album.artist_name else album.service.label
                    row = Adw.ActionRow(title=album.title, subtitle=subtitle)
                    row.set_activatable(True)
                    row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                    row.connect("activated", lambda _r, a=album: self._drill_into(a))
                    attach_context_menu(row, lambda a=album: self._other_row_actions(a))
                    self.other_list.append(row)
            else:
                self.other_list.append(Adw.ActionRow(title="No albums found", sensitive=False))
            self._other_header_label.set_label(f"Albums by {artist.name}")
            self._other_popular_button.set_label(
                "Most popular" if artist.service == Service.QOBUZ else "Top songs"
            )
            self._other_popular_button.set_visible(True)
            self._other_back_bar.set_visible(True)
            self.content_stack.set_visible_child_name("other")

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't load albums: {exc}"))

    def _on_show_artist_top_tracks_clicked(self, _button: Gtk.Button) -> None:
        artist = self._showing_artist
        if artist is None:
            return
        provider = self.state.providers.get(artist.service)
        if provider is None:
            self.state.toast(f"No provider configured for {artist.service.label}")
            return

        def work() -> list[Track]:
            return provider.get_artist_top_tracks(artist.id)

        def done(tracks: list[Track]) -> None:
            self._showing_artist_top_tracks_for = artist
            self._showing_similar_to = None
            replace_tracks(self.track_store, tracks)
            label = "Most popular" if artist.service == Service.QOBUZ else "Top songs"
            self._similar_label.set_label(f"{label} · {artist.name}")
            self._back_bar.set_visible(True)
            self.content_stack.set_visible_child_name("tracks")

        def on_error(exc: BaseException) -> None:
            if isinstance(exc, NotSupportedError):
                self.state.toast(f"{artist.service.label} doesn't support this for artists.")
            else:
                self.state.toast(f"Couldn't load top tracks: {exc}")

        run_async(work, done, on_error)

    def _on_other_back_clicked(self, _button: Gtk.Button) -> None:
        self._other_back_bar.set_visible(False)
        self._other_popular_button.set_visible(False)
        self._showing_artist = None
        self._restore_search_results()

    # -- track actions ------------------------------------------------------

    def _update_action_sensitivity(self) -> None:
        tracks = selected_tracks(self.track_selection)
        self.add_button.set_sensitive(bool(tracks))
        self.find_button.set_sensitive(len(tracks) == 1)
        self.similar_button.set_sensitive(len(tracks) == 1)
        self.play_button.set_sensitive(len(tracks) == 1)

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

    def _on_play_device_clicked(self, button: Gtk.Button) -> None:
        tracks = selected_tracks(self.track_selection)
        if len(tracks) != 1:
            return
        self._open_device_popover(button, tracks[0])

    def _open_device_popover(self, parent: Gtk.Widget, track: Track) -> None:
        devices = self.state.playback_targets()
        popover = Gtk.Popover()
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        if not devices:
            listbox.append(
                Adw.ActionRow(
                    title="No devices yet",
                    subtitle="Add one on the Devices page",
                    sensitive=False,
                )
            )
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
        scroller = Gtk.ScrolledWindow(child=listbox, max_content_height=320,
                                       propagate_natural_height=True, width_request=280)
        popover.set_child(scroller)
        popover.set_parent(parent)
        popover.connect("closed", lambda p: p.unparent())
        popover.popup()

    def _play_track_on_device(self, track: Track, host: str, name: str) -> None:
        self.state.toast(f"Starting “{track.title}” on {name}…")

        def work() -> None:
            self.state.play_track_on_device(track, host)

        def done(_result: None) -> None:
            self.state.toast(f"Playing “{track.title}” on {name}")

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't play on {name}: {exc}"))

    def _on_find_other_clicked(self, _button: Gtk.Button) -> None:
        tracks = selected_tracks(self.track_selection)
        if len(tracks) != 1:
            return
        self._find_other_for_track(tracks[0])

    def _find_other_for_track(self, source: Track) -> None:
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
            self._showing_artist_top_tracks_for = None
            replace_tracks(self.track_store, similar)
            self._similar_label.set_label(f"Similar to “{track.title}” by {track.artist_name}")
            self._back_bar.set_visible(True)
            self.content_stack.set_visible_child_name("tracks")

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't find similar tracks: {exc}"))

    # -- context menus: right-click actions for tracks and other-kind rows -------
    #
    # Additive to the toolbar buttons above, not a replacement -- these open
    # the shared recommender-backed "Similar music" dialog (similar_dialog.py)
    # rather than reusing this page's own inline similar-tracks view, since
    # that view is keyed to the single-selection toolbar flow and its
    # provider-native `similar_tracks` call, not the recommender.

    def _similar_target_provider(self, service: Service) -> object | None:
        """The entity's own provider if configured, else the first available one.

        Used where the fetch only needs *somewhere* to resolve candidate
        matches against (tracks, artists) -- as opposed to album/playlist
        rows, whose fetch must call ``get_album_tracks``/``get_playlist_tracks``
        against the entity's own native provider and so cannot fall back.
        """
        provider = self.state.providers.get(service)
        if provider is not None:
            return provider
        return next(iter(self.state.providers.values()), None)

    def _open_similar(self, title: str, fetch: Callable[[], list]) -> None:
        if self.state.recommender is None:
            self.state.toast("Recommendations aren't available.")
            return
        present_similar(self, self.state, title=title, fetch=fetch)

    def _track_row_actions(self, track: Track) -> list[tuple[str, Callable[[], None]]]:
        actions: list[tuple[str, Callable[[], None]]] = [
            ("Play on Device", lambda: self._open_device_popover(self.column_view, track)),
            ("Add to Playlist…", lambda: self._open_playlist_popover(self.column_view, [track])),
        ]
        provider = self._similar_target_provider(track.service)
        if provider is not None:
            actions.append((
                "Show Similar",
                lambda: self._open_similar(
                    f"Similar to {track.title}",
                    lambda: self.state.recommender.similar_to_tracks([track], provider, limit=40),
                ),
            ))
        other = Service.QOBUZ if track.service == Service.YTMUSIC else Service.YTMUSIC
        if other in self.state.providers:
            actions.append(("Find on Other Service", lambda: self._find_other_for_track(track)))
        return actions

    def _other_row_actions(self, item: Album | Artist | Playlist) -> list[tuple[str, Callable[[], None]]]:
        actions: list[tuple[str, Callable[[], None]]] = []
        # Play on Device / Add to Playlist need the item's own native
        # provider -- get_album_tracks/get_artist_top_tracks/get_playlist_tracks
        # only resolve against the service the id came from, no fallback.
        native_provider = self.state.providers.get(item.service)
        if isinstance(item, Artist):
            label = item.name
            fetch_tracks = (lambda p=native_provider: p.get_artist_top_tracks(item.id)) if native_provider else None
        elif isinstance(item, Album):
            label = item.title
            fetch_tracks = (lambda p=native_provider: p.get_album_tracks(item.id)) if native_provider else None
        else:
            label = item.title
            fetch_tracks = (lambda p=native_provider: p.get_playlist_tracks(item.id)) if native_provider else None
        if fetch_tracks is not None:
            actions.append((
                "Play on Device",
                lambda: play_collection_on_device(self.other_list, self.state, label=label, fetch_tracks=fetch_tracks),
            ))
            actions.append((
                "Add to Playlist…",
                lambda: add_collection_to_playlist(self.other_list, self.state, label=label, fetch_tracks=fetch_tracks),
            ))

        if isinstance(item, Artist):
            provider = self._similar_target_provider(item.service)
            if provider is not None:
                actions.append((
                    "Show Similar",
                    lambda: self._open_similar(
                        f"Similar to {item.name}",
                        lambda: self.state.recommender.similar_to_artist(item.name, provider, limit=40),
                    ),
                ))
        elif isinstance(item, Album):
            # get_album_tracks needs the album's own native provider -- unlike
            # tracks/artists above, there is no sensible fallback here.
            if native_provider is not None:
                actions.append((
                    "Show Similar",
                    lambda: self._open_similar(
                        f"Similar to {item.title}",
                        lambda: self.state.recommender.similar_to_tracks(
                            native_provider.get_album_tracks(item.id), native_provider, limit=40
                        ),
                    ),
                ))
        elif isinstance(item, Playlist):
            if native_provider is not None:
                actions.append((
                    "Show Similar",
                    lambda: self._open_similar(
                        f"Similar to {item.title}",
                        lambda: self.state.recommender.expand_playlist(
                            native_provider.get_playlist_tracks(item.id), native_provider, limit=40
                        ),
                    ),
                ))
        actions.append(("Open", lambda: self._drill_into(item)))
        return actions

    def _on_back_to_results(self, _button: Gtk.Button) -> None:
        self._back_bar.set_visible(False)
        self._showing_similar_to = None
        # Top tracks are one step below the artist's album list in this
        # view's hierarchy (artist -> albums -> most popular/top songs), so
        # "back" from here returns to the albums, which has its own back bar
        # all the way out to search results -- never a dead end either way.
        if self._showing_artist_top_tracks_for is not None:
            artist = self._showing_artist_top_tracks_for
            self._showing_artist_top_tracks_for = None
            self._show_artist_albums(artist)
            return
        self._restore_search_results()

    def _restore_search_results(self) -> None:
        if self._last_results is None:
            self.content_stack.set_visible_child_name("empty")
            return
        kind = _KINDS[self.kind_dropdown.get_selected()]
        if kind == "tracks":
            replace_tracks(self.track_store, self._last_results.tracks)
            self.content_stack.set_visible_child_name("tracks")
        else:
            self._show_other_kind(kind, self._last_results)
