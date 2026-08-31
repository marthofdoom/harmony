"""The persistent Now Playing bar along the bottom of the main window.

Reflects and controls ``AppState.playback`` (the one app-wide playback model):
album art + title/artist, transport (prev / play-pause / next), a draggable
seek bar, shuffle + repeat, a capability-gated volume slider, and a device
selector. Everything here is a thin view over ``AppState`` — the bar never
talks to a device directly; it calls the ``playback_*`` methods, which do the
I/O off the main loop, and redraws on the ``playback-changed`` signal.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # noqa: E402

from harmony.tasks import run_async  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402

log = logging.getLogger(__name__)


def _fmt(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return "0:00"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


class NowPlayingBar(Gtk.Box):
    """Bottom transport bar bound to ``state.playback``."""

    def __init__(self, state: AppState) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                         margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
        self.add_css_class("toolbar")
        self.state = state
        self._art_url: str | None = None
        self._track_key: object = None
        self._seeking = False
        self._syncing = False  # guard programmatic set_value against the change handlers
        self._pending_seek: int | None = None
        self._seek_commit_id = 0
        # Locally-interpolated position so the seek bar advances every second
        # instead of jumping on the ~3s device poll (reconciled on each poll).
        self._interp_pos: float | None = None
        self._devices: list = []
        self._art_cache: dict[str, object] = {}

        # -- now playing: art + title/artist --------------------------------
        self._art = Gtk.Image.new_from_icon_name("emblem-music-symbolic")
        self._art.set_pixel_size(44)
        self.append(self._art)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        meta.set_size_request(150, -1)
        self._title = Gtk.Label(xalign=0.0, ellipsize=3, label="Nothing playing")
        self._title.add_css_class("heading")
        self._artist = Gtk.Label(xalign=0.0, ellipsize=3)
        self._artist.add_css_class("dim-label")
        self._artist.add_css_class("caption")
        meta.append(self._title)
        meta.append(self._artist)
        self.append(meta)

        # -- transport ------------------------------------------------------
        self._prev = Gtk.Button.new_from_icon_name("media-skip-backward-symbolic")
        self._prev.add_css_class("flat")
        self._prev.set_tooltip_text("Previous")
        self._prev.connect("clicked", lambda *_a: self.state.playback_previous())
        self._play = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        self._play.add_css_class("circular")
        self._play.connect("clicked", lambda *_a: self.state.playback_toggle_pause())
        self._next = Gtk.Button.new_from_icon_name("media-skip-forward-symbolic")
        self._next.add_css_class("flat")
        self._next.set_tooltip_text("Next")
        self._next.connect("clicked", lambda *_a: self.state.playback_next())
        for button in (self._prev, self._play, self._next):
            self.append(button)

        # -- seek bar -------------------------------------------------------
        self._pos = Gtk.Label(label="0:00")
        self._pos.add_css_class("caption")
        self.append(self._pos)
        self._seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self._seek.set_hexpand(True)
        self._seek.set_draw_value(False)
        # ``change-value`` fires for every user interaction (drag, trough click,
        # scroll, keyboard) with the proposed value, and -- unlike a competing
        # GestureClick -- never gets swallowed by the scale's own drag gesture.
        # We debounce the actual seek so a drag doesn't spam the device.
        self._seek.connect("change-value", self._on_seek_change)
        self.append(self._seek)
        self._dur = Gtk.Label(label="0:00")
        self._dur.add_css_class("caption")
        self.append(self._dur)

        # -- shuffle + repeat ----------------------------------------------
        self._shuffle = Gtk.ToggleButton(icon_name="media-playlist-shuffle-symbolic")
        self._shuffle.add_css_class("flat")
        self._shuffle.set_tooltip_text("Shuffle")
        self._shuffle.connect("toggled", self._on_shuffle_toggled)
        self.append(self._shuffle)
        self._repeat = Gtk.Button.new_from_icon_name("media-playlist-repeat-symbolic")
        self._repeat.add_css_class("flat")
        self._repeat.set_tooltip_text("Repeat: off")
        self._repeat.connect("clicked", self._on_repeat_clicked)
        self.append(self._repeat)

        # -- volume ---------------------------------------------------------
        self._vol_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        vol_icon = Gtk.Image.new_from_icon_name("audio-volume-high-symbolic")
        self._vol_box.append(vol_icon)
        self._volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self._volume.set_size_request(90, -1)
        self._volume.set_draw_value(False)
        self._volume.connect("change-value", self._on_volume_change)
        self._vol_box.append(self._volume)
        self.append(self._vol_box)

        # -- device selector ------------------------------------------------
        self._devices_model = Gtk.StringList.new([])
        self._device_drop = Gtk.DropDown(model=self._devices_model)
        self._device_drop.set_tooltip_text("Playing to")
        self._device_drop.connect("notify::selected", self._on_device_selected)
        self.append(self._device_drop)

        state.connect("playback-changed", lambda *_a: self._render())
        state.connect("devices-changed", lambda *_a: self._reload_devices())
        self._reload_devices()
        self._render()
        # Advance the seek bar once a second between device polls so time reads
        # smooth and monotonic instead of stepping every ~3s.
        self._tick_id = GLib.timeout_add_seconds(1, self._tick)
        self.connect("unrealize", self._on_unrealize)

    def _on_unrealize(self, *_a: object) -> None:
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0
        if self._seek_commit_id:
            GLib.source_remove(self._seek_commit_id)
            self._seek_commit_id = 0

    # -- device selector ----------------------------------------------------

    def _reload_devices(self) -> None:
        self._devices = self.state.playback_targets()
        self._syncing = True
        self._devices_model.splice(0, self._devices_model.get_n_items(),
                                   [d.name for d in self._devices] or ["No devices"])
        active = self.state.playback.active_host
        for i, dev in enumerate(self._devices):
            if dev.host == active:
                self._device_drop.set_selected(i)
                break
        self._syncing = False
        self._device_drop.set_sensitive(bool(self._devices))

    def _on_device_selected(self, drop: Gtk.DropDown, _param: object) -> None:
        if self._syncing:
            return
        index = drop.get_selected()
        if 0 <= index < len(self._devices):
            self.state.playback_set_active_device(self._devices[index].host)

    # -- user actions -------------------------------------------------------

    def _on_seek_change(self, _scale: Gtk.Scale, _scroll: object, value: float) -> bool:
        # While the user is scrubbing, keep the label live and hold off the
        # render loop (``_seeking``); commit the real seek once they settle so a
        # drag across the bar issues one seek, not one per pixel.
        self._seeking = True
        duration = self.state.playback.duration_s or 0
        target = int(value)
        if duration:
            target = min(target, duration)
        self._pending_seek = target
        self._interp_pos = float(target)
        self._pos.set_label(_fmt(target))
        if self._seek_commit_id:
            GLib.source_remove(self._seek_commit_id)
        self._seek_commit_id = GLib.timeout_add(220, self._commit_seek)
        return False  # let the scale move to the new value

    def _commit_seek(self) -> bool:
        self._seek_commit_id = 0
        self._seeking = False
        if self._pending_seek is not None:
            self.state.playback_seek(self._pending_seek)
            self._pending_seek = None
        return GLib.SOURCE_REMOVE

    def _tick(self) -> bool:
        """Advance the seek bar ~1s/second while playing, between device polls."""
        pb = self.state.playback
        if (
            self._seeking
            or pb.state != "playing"
            or pb.track is None
            or not pb.duration_s
            or self._interp_pos is None
        ):
            return GLib.SOURCE_CONTINUE
        self._interp_pos = min(self._interp_pos + 1.0, float(pb.duration_s))
        pos = int(self._interp_pos)
        # Mirror the interpolated position into the model so an unrelated
        # ``playback-changed`` (e.g. a shuffle/repeat toggle) re-rendering from
        # ``pb.position_s`` doesn't rewind the bar to the last poll; real device
        # polls still overwrite this.
        pb.position_s = pos
        self._syncing = True
        self._seek.set_value(pos)
        self._syncing = False
        self._pos.set_label(_fmt(pos))
        return GLib.SOURCE_CONTINUE

    def _on_volume_change(self, _scale: Gtk.Scale, _scroll: object, value: float) -> bool:
        if not self._syncing:
            self.state.playback_set_volume(int(value))
        return False

    def _on_shuffle_toggled(self, button: Gtk.ToggleButton) -> None:
        if not self._syncing:
            self.state.playback_set_shuffle(button.get_active())

    def _on_repeat_clicked(self, _button: Gtk.Button) -> None:
        order = {"off": "all", "all": "one", "one": "off"}
        self.state.playback_set_repeat(order.get(self.state.playback.repeat, "off"))

    # -- render from the model ---------------------------------------------

    def _render(self) -> None:
        pb = self.state.playback
        # Show the bar only once something is (or was) playing.
        self.set_visible(pb.track is not None)
        if pb.track is None:
            return

        track = pb.track
        key = (track.service, track.id)
        if key != self._track_key:
            self._track_key = key
            self._title.set_label(track.title or "Unknown")
            self._artist.set_label(track.artist_name or "")
            self._load_art(getattr(track, "artwork_url", None))

        # Show what "This computer" is actually outputting (bit depth / rate),
        # a live readout of the highest-quality-stream + bit-perfect work.
        quality = self.state.local_audio_label()
        self._art.set_tooltip_text(f"Output: {quality}" if quality else None)

        playing = pb.state == "playing"
        self._play.set_icon_name(
            "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
        )
        self._play.set_tooltip_text("Pause" if playing else "Play")
        self._prev.set_sensitive(pb.has_prev)
        self._next.set_sensitive(pb.has_next)

        # seek bar (don't fight the user mid-drag). Reconcile the local
        # interpolation baseline to the freshly-polled position so drift from
        # the 1s ticker is corrected every poll.
        if not self._seeking:
            duration = pb.duration_s or 0
            position = pb.position_s or 0
            self._interp_pos = float(position)
            self._syncing = True
            self._seek.set_range(0, max(1, duration))
            self._seek.set_value(min(position, duration or position))
            self._syncing = False
            self._seek.set_sensitive(duration > 0)
            self._pos.set_label(_fmt(position))
            self._dur.set_label(_fmt(pb.duration_s))

        # shuffle + repeat
        self._syncing = True
        self._shuffle.set_active(pb.shuffle)
        self._syncing = False
        self._repeat.set_icon_name(
            "media-playlist-repeat-song-symbolic" if pb.repeat == "one"
            else "media-playlist-repeat-symbolic"
        )
        if pb.repeat == "off":
            self._repeat.remove_css_class("accent")
        else:
            self._repeat.add_css_class("accent")
        self._repeat.set_tooltip_text(f"Repeat: {pb.repeat}")

        # volume (only when the device reports it)
        self._vol_box.set_visible(pb.volume_supported)
        if pb.volume_supported and pb.volume is not None and not self._syncing:
            self._syncing = True
            self._volume.set_value(pb.volume)
            self._syncing = False

        # keep the device selector in sync with the active host
        if pb.active_host is not None:
            for i, dev in enumerate(self._devices):
                if dev.host == pb.active_host and self._device_drop.get_selected() != i:
                    self._syncing = True
                    self._device_drop.set_selected(i)
                    self._syncing = False
                    break

    # -- artwork (best effort, off the main loop) ---------------------------

    def _load_art(self, url: str | None) -> None:
        self._art_url = url
        if not url:
            self._art.set_from_icon_name("emblem-music-symbolic")
            return
        cached = self._art_cache.get(url)
        if cached is not None:
            self._art.set_from_paintable(cached)
            return
        self._art.set_from_icon_name("emblem-music-symbolic")

        def work() -> object:
            # Only the network fetch + GdkPixbuf decode run here; GdkPixbuf is
            # thread-safe, but the GdkTexture (a GDK object) must be created on
            # the main thread -- building it on a worker thread segfaults GTK.
            import requests
            from gi.repository import GdkPixbuf

            data = requests.get(url, timeout=8).content
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            return loader.get_pixbuf()

        def done(pixbuf: object) -> None:
            from gi.repository import Gdk

            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            self._art_cache[url] = texture
            if self._art_url == url:  # still the current track
                self._art.set_from_paintable(texture)

        run_async(work, done, lambda exc: log.debug("art load failed for %s: %s", url, exc))
