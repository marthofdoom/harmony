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

log = logging.getLogger(__name__)

# GStreamer is loaded lazily on first LocalPlayer construction, not at import
# time: the module must stay importable where GStreamer's typelib is absent
# (e.g. the offline CI smoke test), and "This computer" playback is optional.
Gst = None


def _ensure_gst() -> object:
    """Import + init GStreamer once, exposing it as the module-level ``Gst``."""
    global Gst
    if Gst is None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst as _Gst

        _Gst.init(None)
        Gst = _Gst
    return Gst


class LocalStatus(NamedTuple):
    """A device-status-shaped snapshot so the queue poller can treat local
    playback exactly like a remote device."""

    # playing | loading | paused | stopped. ``loading`` is "wants to play but the
    # pipeline is still prerolling / buffering / seeking" -- the UI treats it as
    # playing. The state follows the *target* (what the user asked for), never
    # a transient GStreamer state, so a poll that lands mid-transition can't
    # report "stopped" for audio that's about to start.
    state: str
    position_s: int | None
    duration_s: int | None
    volume: int | None


def _bits_from_format(fmt: str) -> int | None:
    """Sample width in bits from a GStreamer raw-audio format (S16LE, S24_32LE,
    F32LE, ...): the first run of digits is the significant bit depth."""
    digits = ""
    for ch in fmt:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return int(digits) if digits else None


def _describe_caps(caps: object) -> str | None:
    """Human-readable summary of a negotiated raw-audio caps, e.g.
    ``"44.1 kHz · 24-bit · 2ch"``. Returns None if the caps aren't parseable."""
    if caps is None or caps.get_size() == 0:
        return None
    st = caps.get_structure(0)
    ok_rate, rate = st.get_int("rate")
    ok_ch, channels = st.get_int("channels")
    fmt = st.get_string("format") or ""
    parts: list[str] = []
    if ok_rate and rate:
        parts.append(f"{rate / 1000:g} kHz")
    bits = _bits_from_format(fmt)
    if bits:
        kind = "float" if fmt.startswith("F") else "bit"
        parts.append(f"{bits}-{kind}")
    if ok_ch and channels:
        parts.append(f"{channels}ch")
    return " · ".join(parts) or None


class LocalPlayer:
    """A minimal GStreamer ``playbin`` wrapped for Harmony's playback model."""

    def __init__(
        self,
        on_eos: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        _ensure_gst()
        self._on_eos = on_eos
        self._on_error = on_error
        # Fired (main loop) when the pipeline's real state changes or buffering
        # starts/ends, so the model follows promptly instead of at the next poll.
        self._on_change = on_change
        # What the user asked for: "playing" | "paused" | "stopped". The pipeline
        # lags this (async state changes, buffering); status() reports the intent.
        self._intent = "stopped"
        self._buffering = False
        # True once the pipeline has really reached PLAYING since the last load.
        # A pipeline that prerolled (PAUSED) but never got there has a sink that
        # was never started, and tearing that down hangs (see _to_null).
        self._started = False
        self._headers: dict[str, str] = {}
        self._sink: object | None = None
        self._logged_format = False
        self._playbin = Gst.ElementFactory.make("playbin", "harmony-local")
        if self._playbin is None:
            raise RuntimeError("GStreamer playbin is unavailable")
        # Prefer a direct PipeWire sink for the bit-perfect path; fall back to
        # playbin's default (autoaudiosink) if pipewiresink isn't present.
        self._sink = Gst.ElementFactory.make("pipewiresink", "harmony-sink")
        if self._sink is not None:
            self._playbin.set_property("audio-sink", self._sink)
        else:
            log.info("pipewiresink unavailable; using GStreamer's default audio sink")
        self._playbin.connect("source-setup", self._on_source_setup)
        bus = self._playbin.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._handle_eos)
        bus.connect("message::error", self._handle_error)
        bus.connect("message::state-changed", self._handle_state_changed)
        bus.connect("message::buffering", self._handle_buffering)

    def _on_source_setup(self, _playbin: Gst.Element, source: Gst.Element) -> None:
        # Pass any CDN request headers (e.g. a YouTube User-Agent) to the HTTP
        # source. Qobuz FLAC URLs need none.
        if self._headers and source.find_property("extra-headers") is not None:
            structure = Gst.Structure.new_empty("extra-headers")
            for key, value in self._headers.items():
                structure.set_value(key, value)
            source.set_property("extra-headers", structure)

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def _handle_eos(self, _bus: Gst.Bus, _msg: Gst.Message) -> None:
        if self._on_eos is not None:
            self._on_eos()

    def _handle_error(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        err, _debug = msg.parse_error()
        log.warning("local player error: %s", err)
        self._intent = "stopped"
        self._buffering = False
        # An errored pipeline can still be prerolled-but-never-started (e.g. a
        # stream loaded paused whose server then dropped): same teardown rule.
        self._to_null()
        if self._on_error is not None:
            self._on_error(str(err))

    def _handle_state_changed(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        # Once the top-level pipeline reaches PLAYING the audio format is
        # negotiated: log the output format once so "highest quality" and any
        # resampling are verifiable from the log (see audio_info()).
        if msg.src is not self._playbin:
            return
        self._notify()
        _old, new, _pending = msg.parse_state_changed()
        if new == Gst.State.PLAYING:
            self._started = True
        if new != Gst.State.PLAYING or self._logged_format:
            return
        info = self.audio_info()
        if info is not None:
            self._logged_format = True
            log.info("Local output negotiated: %s", info)

    def _handle_buffering(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        """Network buffering: while the queue refills, hold the pipeline in
        PAUSED (so the sink doesn't underrun) and resume at 100% -- but only if
        the user still wants playback. The *intent* never changes here, so the
        UI keeps showing "playing" (as "loading") instead of flickering."""
        if self._intent == "stopped":
            return
        try:
            mode = msg.parse_buffering_stats().mode
            if mode == Gst.BufferingMode.LIVE:
                return  # live streams can't be paused to buffer
        except Exception:  # noqa: BLE001 - stats are optional
            pass
        percent = msg.parse_buffering()
        was = self._buffering
        self._buffering = percent < 100
        if self._intent == "playing":
            if self._buffering and not was:
                self._note_started()
                self._playbin.set_state(Gst.State.PAUSED)
            elif was and not self._buffering:
                self._playbin.set_state(Gst.State.PLAYING)
        if was != self._buffering:
            self._notify()

    def _output_caps(self) -> object | None:
        """The raw-audio caps actually negotiated into the audio sink -- what's
        handed to PipeWire. Prefer the real sink pad; fall back to playbin's
        decoded audio pad if a default sink was substituted."""
        pad = None
        if self._sink is not None:
            pad = self._sink.get_static_pad("sink")
        if pad is None:
            try:
                pad = self._playbin.emit("get-audio-pad", 0)
            except (TypeError, AttributeError):
                pad = None
        if pad is None:
            return None
        return pad.get_current_caps()

    def audio_info(self) -> str | None:
        """Negotiated output format summary (e.g. ``"44.1 kHz · 24-bit · 2ch"``),
        or None if nothing is playing / caps aren't available yet."""
        return _describe_caps(self._output_caps())

    # -- transport ----------------------------------------------------------

    @property
    def intent(self) -> str:
        """What the user last asked for: ``playing`` | ``paused`` | ``stopped``."""
        return self._intent

    def load_and_play(self, url: str, headers: dict[str, str] | None = None,
                      paused: bool = False) -> None:
        self._headers = dict(headers or {})
        self._logged_format = False
        self._buffering = False
        self._to_null()
        self._playbin.set_property("uri", url)
        self._intent = "paused" if paused else "playing"
        self._playbin.set_state(Gst.State.PAUSED if paused else Gst.State.PLAYING)

    def pause(self) -> None:
        self._intent = "paused"
        self._note_started()
        self._playbin.set_state(Gst.State.PAUSED)

    def resume(self) -> None:
        self._intent = "playing"
        # Mid-buffering the pipeline is held in PAUSED on purpose; the buffering
        # handler starts it when the queue is full.
        if not self._buffering:
            self._playbin.set_state(Gst.State.PLAYING)

    def _note_started(self) -> None:
        """Record that the pipeline is PLAYING *now*. The bus message that sets
        ``_started`` is dispatched on the main loop, so a pause/stop that follows
        a play before the loop ran would otherwise not know the sink had started."""
        if self._playbin.get_state(0)[1] == Gst.State.PLAYING:
            self._started = True

    def _to_null(self) -> None:
        """Tear the pipeline down to NULL.

        A pipewiresink that prerolled in PAUSED but was never started blocks
        forever when taken back down to READY/NULL, freezing the whole UI on
        Stop / next track. That is any pipeline that never reached PLAYING since
        its load: a track loaded paused, pause pressed while it was still
        prerolling, a buffering hold before the first play, or an errored stream
        (a dropped connection). Starting it first (muted, so nothing is heard,
        and waiting at most half a second) lets it shut down."""
        pb = self._playbin
        self._note_started()
        needs_start = False
        if not self._started:
            _ret, current, pending = pb.get_state(0)
            needs_start = current == Gst.State.PAUSED or pending == Gst.State.PAUSED
        if needs_start:
            pb.set_property("mute", True)
            try:
                pb.set_state(Gst.State.PLAYING)
                pb.get_state(Gst.SECOND // 2)
                pb.set_state(Gst.State.NULL)
            finally:
                pb.set_property("mute", False)
        else:
            pb.set_state(Gst.State.NULL)
        self._started = False

    def stop(self) -> None:
        self._intent = "stopped"
        self._buffering = False
        self._to_null()

    def seek(self, position_s: int) -> bool:
        """Seek to ``position_s``. Returns False when GStreamer refuses (e.g. a
        non-seekable HTTP source that serves no byte ranges), so the caller can
        surface it instead of leaving the bar parked at a position that never
        took."""
        ok = self._playbin.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            max(0, int(position_s)) * Gst.SECOND,
        )
        if not ok:
            log.warning("local seek to %ss refused (stream not seekable)", position_s)
        return bool(ok)

    def set_volume(self, level: int) -> None:
        self._playbin.set_property("volume", max(0, min(100, int(level))) / 100.0)

    def status(self) -> LocalStatus:
        # Report the TARGET, not the instantaneous pipeline state: during an
        # async transition (new URI, seek, buffering) get_state(0) returns the
        # *current* state (READY/PAUSED) while PLAYING is merely pending, which
        # used to read as "stopped" while audio was about to start.
        if self._intent == "playing":
            _ok, gst_state, _pending = self._playbin.get_state(0)
            ready = gst_state == Gst.State.PLAYING and not self._buffering
            state = "playing" if ready else "loading"
        else:
            state = self._intent
        ok_pos, pos = self._playbin.query_position(Gst.Format.TIME)
        ok_dur, dur = self._playbin.query_duration(Gst.Format.TIME)
        volume = int(round(self._playbin.get_property("volume") * 100))
        return LocalStatus(
            state=state,
            position_s=pos // Gst.SECOND if ok_pos and pos >= 0 else None,
            duration_s=dur // Gst.SECOND if ok_dur and dur >= 0 else None,
            volume=volume,
        )
