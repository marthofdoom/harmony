"""The Now Playing page.

Large album art of the current track beside the live **active queue** (the current
track first, then what's actually up next), with the current track lit by the
shared now-playing indicator. Shuffle + repeat act on the queue; double-click a row
to jump to it; right-click to reorder (Move Up/Down), remove, or the usual track
actions. A thin view over ``AppState`` — it reads ``playback`` + ``active_queue()``
and redraws on ``playback-changed``.
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
        self._syncing = False  # guard programmatic control updates from their handlers

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

        # Shuffle + repeat, acting on the active queue (mirrors the Now Playing bar).
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin_top=6)
        self._shuffle = Gtk.ToggleButton(icon_name="media-playlist-shuffle-symbolic")
        self._shuffle.add_css_class("flat")
        self._shuffle.set_tooltip_text("Shuffle")
        self._shuffle.connect("toggled", self._on_shuffle_toggled)
        self._repeat = Gtk.Button.new_from_icon_name("media-playlist-repeat-symbolic")
        self._repeat.add_css_class("flat")
        self._repeat.set_tooltip_text("Repeat: off")
        self._repeat.connect("clicked", self._on_repeat_clicked)
        controls.append(self._shuffle)
        controls.append(self._repeat)
        art_col.append(controls)
        content.append(art_col)

        holder: dict[str, Gtk.Widget] = {}
        self._base_menu = track_menu_builder(state, navigator, lambda: holder["cv"])
        self._cv, self._store, _sel = build_track_column_view(
            on_row_menu=self._queue_row_menu,
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

    # -- queue controls -----------------------------------------------------

    def _on_shuffle_toggled(self, button: Gtk.ToggleButton) -> None:
        if not self._syncing:
            self.state.playback_set_shuffle(button.get_active())

    def _on_repeat_clicked(self, _button: Gtk.Button) -> None:
        order = {"off": "all", "all": "one", "one": "off"}
        self.state.playback_set_repeat(order.get(self.state.playback.repeat, "off"))

    def _queue_row_menu(self, track):
        """Queue reorder/remove ops for the tapped row, then the standard track menu."""
        items: list = []
        queue = self.state.active_queue()
        idx = next((i for i, t in enumerate(queue) if t.key() == track.key()), -1)
        if idx > 1:
            items.append(("Move Up", lambda: self.state.playback_reorder(idx, idx - 1)))
        if 0 < idx < len(queue) - 1:
            items.append(("Move Down", lambda: self.state.playback_reorder(idx, idx + 1)))
        if idx >= 1:
            items.append(("Remove from Queue", lambda: self.state.playback_remove(track)))
        return items + self._base_menu(track)

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

        # Reflect shuffle/repeat state.
        self._syncing = True
        self._shuffle.set_active(pb.shuffle)
        self._syncing = False
        self._repeat.set_icon_name(
            "media-playlist-repeat-song-symbolic" if pb.repeat == "one"
            else "media-playlist-repeat-symbolic")
        self._repeat.set_tooltip_text(f"Repeat: {pb.repeat}")
        if pb.repeat == "off":
            self._repeat.remove_css_class("accent")
        else:
            self._repeat.add_css_class("accent")

        # The live active queue (current track first, then what's up next). Rebuild
        # only when it actually changes, so selection/scroll survive status polls;
        # the shared indicator column tracks the current track on its own.
        queue = self.state.active_queue()
        sig = (len(queue), tuple(t.key() for t in queue))
        if sig != self._queue_sig:
            self._queue_sig = sig
            replace_tracks(self._store, queue)
