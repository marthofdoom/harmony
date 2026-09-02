"""The Now Playing page.

Large album art of the current track beside the full track list of the playing
collection (the whole album/playlist, not just what's left), with the current
track lit by the shared now-playing indicator. Double-click a row to play from
there; right-click for the usual track menu. A thin view over ``AppState`` —
it reads ``playback`` + ``current_queue()`` and redraws on ``playback-changed``.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from harmony.ui.detail_widgets import track_menu_builder  # noqa: E402
from harmony.ui.entity_nav import Navigator, load_artwork_into  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.widgets import build_track_column_view, replace_tracks  # noqa: E402


class NowPlayingPage(Gtk.Box):
    """Now Playing: big art + the current collection's track list."""

    def __init__(self, state: AppState, navigator: Navigator) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.state = state
        self.navigator = navigator
        self._art_url: str | None = None
        self._track_key: object = None
        self._queue_sig: object = None

        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        self.append(self._stack)

        # -- content: art column + track list ------------------------------
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18,
                          margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)

        art_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          valign=Gtk.Align.START)
        art_col.set_size_request(300, -1)
        self._art = Gtk.Image.new_from_icon_name("emblem-music-symbolic")
        self._art.set_pixel_size(280)
        self._art.add_css_class("card")
        art_col.append(self._art)
        self._title = Gtk.Label(xalign=0.0, wrap=True, label="")
        self._title.add_css_class("title-2")
        self._artist = Gtk.Label(xalign=0.0, wrap=True, label="")
        self._artist.add_css_class("dim-label")
        art_col.append(self._title)
        art_col.append(self._artist)
        content.append(art_col)

        holder: dict[str, Gtk.Widget] = {}
        self._cv, self._store, _sel = build_track_column_view(
            on_row_menu=track_menu_builder(state, navigator, lambda: holder["cv"]),
            state=state,
            on_row_activate=lambda t: state.playback_play_from(t),
        )
        holder["cv"] = self._cv
        scroller = Gtk.ScrolledWindow(child=self._cv, vexpand=True, hexpand=True)
        content.append(scroller)
        self._stack.add_named(content, "content")

        # -- empty state ---------------------------------------------------
        empty = Adw.StatusPage(
            icon_name="emblem-music-symbolic",
            title="Nothing playing",
            description="Play a track, album, or playlist and it shows up here.",
        )
        self._stack.add_named(empty, "empty")

        state.connect("playback-changed", lambda *_a: self._render())
        self._render()

    def _render(self) -> None:
        pb = self.state.playback
        if pb.track is None:
            self._stack.set_visible_child_name("empty")
            return
        self._stack.set_visible_child_name("content")

        track = pb.track
        key = (track.service, track.id)
        if key != self._track_key:
            self._track_key = key
            self._title.set_label(track.title or "Unknown")
            self._artist.set_label(track.artist_name or "")
            art = getattr(track, "artwork_url", None)
            if art != self._art_url:
                self._art_url = art
                load_artwork_into(self._art, art)

        # Rebuild the list only when the collection itself changes — not on every
        # 3s status poll — so selection and scroll position survive. The shared
        # indicator column tracks the current track on its own.
        queue = self.state.current_queue()
        sig = (pb.collection_key, len(queue),
               queue[0].key() if queue else None,
               queue[-1].key() if queue else None)
        if sig != self._queue_sig:
            self._queue_sig = sig
            replace_tracks(self._store, queue)
