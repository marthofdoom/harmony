"""In-app local audio playback via GStreamer (the "This computer" target).

GStreamer ships in the runtime (no bundling), and ``pipewiresink`` routes
straight to PipeWire, so this reuses the same bit-perfect story as casting:
output at the source rate when the sink allows it, resample gracefully
otherwise. This lives in the UI layer, not the engine, because GStreamer is a
gi/GObject library — the engine layer stays gi-free (see docs/ARCHITECTURE.md).

A single ``LocalPlayer`` is owned by ``AppState`` and driven by its playback
model: ``load_and_play`` a resolved stream URL, transport controls, and a
status snapshot the queue poller reads just like a device's status.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import NamedTuple

import gi

gi.require_version("Gst", "1.0")

from gi.repository import Gst  # noqa: E402

log = logging.getLogger(__name__)


class LocalStatus(NamedTuple):
    """A device-status-shaped snapshot so the queue poller can treat local
    playback exactly like a remote device."""

    state: str  # playing | paused | stopped
    position_s: int | None
    duration_s: int | None
    volume: int | None


class LocalPlayer:
    """A minimal GStreamer ``playbin`` wrapped for Harmony's playback model."""

    def __init__(
        self,
        on_eos: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        Gst.init(None)
        self._on_eos = on_eos
        self._on_error = on_error
        self._headers: dict[str, str] = {}
        self._playbin = Gst.ElementFactory.make("playbin", "harmony-local")
        if self._playbin is None:
            raise RuntimeError("GStreamer playbin is unavailable")
        # Prefer a direct PipeWire sink for the bit-perfect path; fall back to
        # playbin's default (autoaudiosink) if pipewiresink isn't present.
        sink = Gst.ElementFactory.make("pipewiresink", "harmony-sink")
        if sink is not None:
            self._playbin.set_property("audio-sink", sink)
        else:
            log.info("pipewiresink unavailable; using GStreamer's default audio sink")
        self._playbin.connect("source-setup", self._on_source_setup)
        bus = self._playbin.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._handle_eos)
        bus.connect("message::error", self._handle_error)

    def _on_source_setup(self, _playbin: Gst.Element, source: Gst.Element) -> None:
        # Pass any CDN request headers (e.g. a YouTube User-Agent) to the HTTP
        # source. Qobuz FLAC URLs need none.
        if self._headers and source.find_property("extra-headers") is not None:
            structure = Gst.Structure.new_empty("extra-headers")
            for key, value in self._headers.items():
                structure.set_value(key, value)
            source.set_property("extra-headers", structure)

    def _handle_eos(self, _bus: Gst.Bus, _msg: Gst.Message) -> None:
        if self._on_eos is not None:
            self._on_eos()

    def _handle_error(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        err, _debug = msg.parse_error()
        log.warning("local player error: %s", err)
        self._playbin.set_state(Gst.State.NULL)
        if self._on_error is not None:
            self._on_error(str(err))

    # -- transport ----------------------------------------------------------

    def load_and_play(self, url: str, headers: dict[str, str] | None = None) -> None:
        self._headers = dict(headers or {})
        self._playbin.set_state(Gst.State.NULL)
        self._playbin.set_property("uri", url)
        self._playbin.set_state(Gst.State.PLAYING)

    def pause(self) -> None:
        self._playbin.set_state(Gst.State.PAUSED)

    def resume(self) -> None:
        self._playbin.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        self._playbin.set_state(Gst.State.NULL)

    def seek(self, position_s: int) -> None:
        self._playbin.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            max(0, int(position_s)) * Gst.SECOND,
        )

    def set_volume(self, level: int) -> None:
        self._playbin.set_property("volume", max(0, min(100, int(level))) / 100.0)

    def status(self) -> LocalStatus:
        _ok, gst_state, _pending = self._playbin.get_state(0)
        state = {
            Gst.State.PLAYING: "playing",
            Gst.State.PAUSED: "paused",
        }.get(gst_state, "stopped")
        ok_pos, pos = self._playbin.query_position(Gst.Format.TIME)
        ok_dur, dur = self._playbin.query_duration(Gst.Format.TIME)
        volume = int(round(self._playbin.get_property("volume") * 100))
        return LocalStatus(
            state=state,
            position_s=pos // Gst.SECOND if ok_pos and pos >= 0 else None,
            duration_s=dur // Gst.SECOND if ok_dur and dur >= 0 else None,
            volume=volume,
        )
