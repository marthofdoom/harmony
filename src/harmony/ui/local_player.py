"""In-app local audio playback via GStreamer (the "This computer" target).

GStreamer ships in the runtime (no bundling), and ``pipewiresink`` routes
straight to PipeWire, so this reuses the same bit-perfect story as casting:
output at the source rate when the sink allows it, resample gracefully
otherwise. This lives in the UI layer, not the engine, because GStreamer is a
gi/GObject library — the engine layer stays gi-free (see docs/ARCHITECTURE.md).

A single ``LocalPlayer`` is owned by ``AppState`` and driven by its playback
model: ``load_and_play`` a resolved stream URL, transport controls, and a
status snapshot the queue poller reads just like a device's status.

Two pieces of policy live here besides plain playback:

* **Buffering** (:func:`buffering_plan`): a network track is fetched *ahead* of
  the play head -- a progressive download into a temp file for tracks with a
  known length (the Library, Qobuz), a large look-ahead queue otherwise
  (YouTube) -- so a Wi-Fi hiccup never reaches the speakers.
* **Bit-perfect output** (:meth:`LocalPlayer.set_bit_perfect`): software volume
  pinned at 100% and the PipeWire stream opened with no up/down-mixing and the
  graph rate locked to the track's own rate. ``harmony.audio.bitperfect`` judges
  whether that actually held.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

from harmony.audio.bitperfect import AudioFormat
from harmony.audio.pipewire import STREAM_NODE_NAME

log = logging.getLogger(__name__)

# GstPlayFlags (gst-plugins-base, playbin): the two we toggle.
_FLAG_SOFT_VOLUME = 0x10
_FLAG_DOWNLOAD = 0x80
# playbin's default flags (audio + soft volume + the video/text defaults).
_DEFAULT_FLAGS = 0x617

_SECOND_NS = 1_000_000_000
_PERCENT_MAX = 1_000_000  # GST_FORMAT_PERCENT_MAX
_MIB = 1024 * 1024


@dataclass(frozen=True)
class BufferingPlan:
    """How a source is buffered. ``mode``:

    * ``none``     -- a local file: nothing to buffer.
    * ``download`` -- progressive download into a temp file (seekable HTTP with a
      known length). The whole track is fetched as fast as the network allows;
      playback starts once ``window_bytes`` (a second or two of audio, see
      :func:`start_window_bytes`) are ahead, and only re-buffers if the download
      falls to ``low_percent`` of that window -- i.e. only if the network stalls
      for longer than everything already fetched.
    * ``stream``   -- a ``queue2`` look-ahead of ``stream_duration_s`` seconds /
      ``stream_size_bytes``; starts at ``stream_high`` of it, re-buffers below
      ``stream_low`` (fractions). For streams we can't download (YouTube).
    """

    mode: str = "none"
    start_window_s: int = 2
    # The buffer's *byte* size is what sets how much must arrive before playback
    # starts (measured: first audio = window_bytes / network speed; the time cap
    # is only consulted once a bitrate is known, which a downloadbuffer never has).
    # So it is kept small -- about two seconds of the track's own bitrate -- and the
    # resilience comes from the download running far ahead behind it, not from a
    # big start window (a 4 MiB one cost 4-6 s at 1 MB/s or 100 KB/s).
    window_bytes: int = 384 * 1024
    low_percent: int = 20
    stream_duration_s: int = 30
    stream_size_bytes: int = 32 * _MIB  # 30 s of 24/192 FLAC is ~21 MB
    stream_low: float = 0.03
    stream_high: float = 0.15

    @property
    def download(self) -> bool:
        return self.mode == "download"


_NO_BUFFERING = BufferingPlan(mode="none")
_STREAM_BUFFERING = BufferingPlan(mode="stream")
_DOWNLOAD_BUFFERING = BufferingPlan(mode="download")

_LIVE_HOSTS = ("googlevideo.com", "youtube.com", "youtu.be")


def start_window_bytes(mime: str | None = None, quality: dict | None = None) -> int:
    """Bytes of a download to have ahead before playback starts: ~2 s of the
    track's own bitrate, from its quality (kbps, or PCM rate x depth for a
    lossless file ~60 % compressed) or, failing that, its MIME type; 64 KiB to
    1 MiB. (24/192 FLAC is ~1.2 MB/s, a 320 kbps MP3 40 KB/s.)"""
    per_second = 0.0
    q = quality or {}
    if q.get("bitrate"):
        per_second = float(q["bitrate"]) * 125
    elif q.get("lossless") and q.get("sample_rate") and q.get("bit_depth"):
        per_second = float(q["sample_rate"]) * float(q["bit_depth"]) * (q.get("channels") or 2) / 8 * 0.6
    elif (mime or "").lower().startswith(("audio/mpeg", "audio/aac", "audio/mp4", "audio/ogg", "audio/opus",
                                          "audio/webm")):
        per_second = 40_000.0
    if not per_second:
        return 384 * 1024
    return int(min(max(per_second * 2, 64 * 1024), _MIB))


def buffering_plan(url: str, mime: str | None = None, quality: dict | None = None) -> BufferingPlan:
    """Pick the buffering strategy for ``url`` (pure; unit-tested).

    Local files need none. A YouTube CDN URL gets the large look-ahead queue
    (its URLs are throttled and signed, so fetching the whole file ahead isn't
    reliable). Every other HTTP source -- the Library, a peer's ``/stream/``,
    Qobuz's FLAC -- is a seekable file with a known length, so it is downloaded
    ahead; GStreamer itself falls back to the queue for one that turns out not
    to be seekable. ``mime``/``quality`` (the stream's quality dict) size the
    download's start window.
    """
    parts = urlparse(url or "")
    if parts.scheme not in ("http", "https"):
        return _NO_BUFFERING
    host = (parts.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in _LIVE_HOSTS):
        return _STREAM_BUFFERING
    return replace(_DOWNLOAD_BUFFERING, window_bytes=start_window_bytes(mime, quality))


def stream_buffer_dir() -> Path:
    """Where GStreamer's progressive-download temp files live: the app's cache
    dir (inside the Flatpak that is ``~/.var/app/<id>/cache``), never the
    sandbox's RAM-backed ``/tmp`` -- a 24/192 FLAC is tens of megabytes."""
    from harmony.config import cache_dir

    return cache_dir() / "stream-buffer"


def sweep_stream_buffers() -> int:
    """Delete download temp files a crashed run left behind; returns how many.
    GStreamer removes its own at the end of each track (``temp-remove``)."""
    directory = stream_buffer_dir()
    removed = 0
    if directory.is_dir():
        for entry in directory.glob("dl-*"):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


#: ``pipewiresink`` ``node.latency`` (time-based, so rate-independent): a music
#: player wants headroom, not the graph's 21 ms default. 2048 frames at 48 kHz is
#: ~43 ms. Measured against a private PipeWire with every core busy: unset/1024
#: showed 4 xruns in 20 s, 2048 and 4096 showed 2, with no silent gap in the sink
#: monitor in any case -- so the smaller of the two that helps. It only matters when
#: this is the only stream with a latency request (the graph runs at the *smallest*
#: request), and then it makes a no-preference app share a 43 ms quantum instead of 21 ms.
_STREAM_LATENCY = "2048/48000"
#: Extra time (ns) the sink may take to render a buffer: the pipeline's cushion
#: against scheduling jitter in the decode thread.
_SINK_DEADLINE_NS = 100_000_000

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


def _caps_format(caps: object) -> AudioFormat | None:
    """The :class:`AudioFormat` of raw-audio caps (None if unreadable)."""
    if caps is None or caps.get_size() == 0:
        return None
    st = caps.get_structure(0)
    ok_rate, rate = st.get_int("rate")
    ok_ch, channels = st.get_int("channels")
    return AudioFormat(rate=rate if ok_rate else None, format=st.get_string("format"),
                       channels=channels if ok_ch else None)


class LocalPlayer:
    """A minimal GStreamer ``playbin`` wrapped for Harmony's playback model."""

    # Class-level defaults so a player built without __init__ (tests drive a fake
    # playbin) behaves as "not bit-perfect, nothing buffered".
    _bit_perfect = False
    _plan: BufferingPlan = _NO_BUFFERING
    _base_flags = _DEFAULT_FLAGS
    _url: str | None = None
    _mime: str | None = None
    _quality: dict | None = None

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
        # A seek asked for while the pipeline sits in PAUSED without ever having
        # played: seeking it (FLUSH) never returns on pipewiresink, so the target
        # is held here and applied when the pipeline first reaches PLAYING.
        self._pending_seek_s: int | None = None
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
        self._playbin.connect("deep-element-added", self._on_element_added)
        try:
            self._base_flags = int(self._playbin.get_property("flags"))
        except (TypeError, ValueError):
            self._base_flags = _DEFAULT_FLAGS
        if self._sink is not None and self._sink.find_property("processing-deadline") is not None:
            self._sink.set_property("processing-deadline", _SINK_DEADLINE_NS)
        self._apply_stream_properties()
        # Leftovers of a crashed run (GStreamer deletes its own at each track end).
        sweep_stream_buffers()
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

    def _on_element_added(self, _playbin: Gst.Element, _bin: Gst.Bin, element: Gst.Element) -> None:
        """Tune the buffering elements playbin builds for the current source.

        playbin forwards one ``buffer-duration``/``buffer-size`` to whichever
        buffer it creates; that suits the look-ahead queue but would make the
        progressive-download buffer wait for the whole window before starting, so
        each element is set explicitly here."""
        factory = element.get_factory()
        name = factory.get_name() if factory is not None else ""
        plan = self._plan
        try:
            if name == "downloadbuffer":
                directory = stream_buffer_dir()
                directory.mkdir(parents=True, exist_ok=True)
                element.set_property("temp-template", str(directory / "dl-XXXXXX"))
                element.set_property("temp-remove", True)
                element.set_property("max-size-time", plan.start_window_s * _SECOND_NS)
                element.set_property("max-size-bytes", plan.window_bytes)
                element.set_property("low-percent", plan.low_percent)
                element.set_property("high-percent", 99)
            elif name == "queue2" and plan.mode != "none":
                # Also the fallback for a download source the server turned out
                # not to serve with a length: without this its 99 % high watermark
                # of a 30 s / 32 MiB queue means a 5-20 s start.
                element.set_property("low-watermark", plan.stream_low)
                element.set_property("high-watermark", plan.stream_high)
            elif name == "audioconvert" and self._bit_perfect:
                # audioconvert's default is TPDF dither on any bit-depth reduction.
                # Bit-perfect: none, ever (outside bit-perfect the default stays).
                element.set_property("dithering", 0)
                element.set_property("noise-shaping", 0)
        except Exception:  # noqa: BLE001 - tuning is best-effort; playback must not depend on it
            log.debug("could not tune %s", name, exc_info=True)

    def _apply_buffering(self, plan: BufferingPlan) -> None:
        """Configure playbin for ``plan`` before the next URI is set."""
        self._plan = plan
        flags = self._base_flags
        flags = flags | _FLAG_DOWNLOAD if plan.download else flags & ~_FLAG_DOWNLOAD
        self._playbin.set_property("flags", flags)
        if plan.mode == "none":
            self._playbin.set_property("buffer-duration", -1)
            self._playbin.set_property("buffer-size", -1)
        else:
            self._playbin.set_property("buffer-duration", plan.stream_duration_s * _SECOND_NS)
            self._playbin.set_property("buffer-size", plan.stream_size_bytes)

    # -- bit-perfect output -----------------------------------------------------

    @property
    def bit_perfect(self) -> bool:
        return self._bit_perfect

    def set_bit_perfect(self, enabled: bool) -> bool:
        """Switch bit-perfect output. Volume is pinned to 100% at once; the
        PipeWire stream properties (and the no-dither ``audioconvert``) apply
        when the pipeline next connects. Returns True when something is loaded
        that must be *restarted* to pick them up -- the caller reloads it through
        the normal play path (a fresh stream URL: the old one may have expired)."""
        enabled = bool(enabled)
        if enabled == self._bit_perfect:
            return False
        self._bit_perfect = enabled
        if enabled:
            self._playbin.set_property("volume", 1.0)
            self._playbin.set_property("mute", False)
        self._apply_stream_properties()
        return self._intent != "stopped" and bool(self._url)

    def current_position_s(self) -> int:
        """Where playback is (or will resume): a deferred seek wins over the
        pipeline's own position, which is 0 until it has played."""
        if self._pending_seek_s is not None:
            return int(self._pending_seek_s)
        return self.position_s() or 0

    def _apply_stream_properties(self) -> None:
        """The PipeWire stream properties for the current mode (sink must be NULL).

        Always: a findable node name and a latency with headroom. Bit-perfect:
        no up/down-mixing (``node.dont-remix``) and the graph's rate locked to the
        track's (``node.lock-rate``) so another app starting later can't drag the
        output to its own rate. Resampling is deliberately left *allowed* -- a
        track the device can't run natively still plays, and the indicator says
        so -- and the rate is never forced past PipeWire's ``allowed-rates``."""
        if self._sink is None:
            return
        props = Gst.Structure.new_empty("props")
        props.set_value("node.name", STREAM_NODE_NAME)
        if _STREAM_LATENCY:
            props.set_value("node.latency", _STREAM_LATENCY)
        if self._bit_perfect:
            props.set_value("node.dont-remix", "true")
            props.set_value("node.lock-rate", "true")
        self._sink.set_property("stream-properties", props)

    def decoded_format(self) -> AudioFormat | None:
        """What the decoder produced (before anything between it and the sink)."""
        try:
            pad = self._playbin.emit("get-audio-pad", 0)
        except (TypeError, AttributeError):
            pad = None
        return _caps_format(pad.get_current_caps()) if pad is not None else None

    def sink_format(self) -> AudioFormat | None:
        """What the app actually handed to PipeWire."""
        return _caps_format(self._output_caps())

    def position_s(self) -> int | None:
        ok, pos = self._playbin.query_position(Gst.Format.TIME)
        return int(pos // Gst.SECOND) if ok and pos >= 0 else None

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
            self._apply_pending_seek()
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
                      paused: bool = False, start_s: int = 0, mime: str | None = None,
                      quality: dict | None = None, plan: BufferingPlan | None = None) -> None:
        """Load ``url``. ``start_s`` > 0 starts the track there (applied once it
        first reaches PLAYING, never against an unstarted pipeline). ``mime`` and
        ``quality`` describe the stream, when known (they size the buffering plan)."""
        self._headers = dict(headers or {})
        self._url, self._mime, self._quality = url, mime, quality
        self._logged_format = False
        self._buffering = False
        self._to_null()
        self._pending_seek_s = int(start_s) if start_s and start_s > 0 else None
        if plan is None:
            plan = buffering_plan(url, mime, quality)
            if plan.download and start_s and start_s > 0:
                # Resuming mid-track needs data the download hasn't fetched: the
                # queue plan seeks with an ordinary Range request (see _seek_now).
                plan = _STREAM_BUFFERING
        self._apply_buffering(plan)
        self._apply_stream_properties()
        if self._bit_perfect:
            self._playbin.set_property("volume", 1.0)
            self._playbin.set_property("mute", False)
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
            if not needs_start and self._sink is not None:
                # The bin can already read READY/PLAYING-pending while the sink
                # itself still sits prerolled in PAUSED: that is the hang too.
                _r, s_cur, s_pend = self._sink.get_state(0)
                needs_start = s_cur == Gst.State.PAUSED or s_pend == Gst.State.PAUSED
        if needs_start:
            # Nothing may be heard. Mute AND volume 0: both act upstream of the
            # sink, so they silence what is decoded from now on; the one buffer
            # the sink already prerolled with cannot be recalled (flushing it or
            # shifting its timestamp makes the pipewiresink teardown hang again),
            # so the exposure is bounded by keeping the start as short as it can
            # be -- the state change of an already-prerolled sink completes at once.
            volume = pb.get_property("volume")
            pb.set_property("mute", True)
            pb.set_property("volume", 0.0)
            try:
                pb.set_state(Gst.State.PLAYING)
                pb.get_state(Gst.SECOND // 2)
                pb.set_state(Gst.State.NULL)
            finally:
                pb.set_property("volume", volume)
                pb.set_property("mute", False)
        else:
            pb.set_state(Gst.State.NULL)
        self._started = False
        self._pending_seek_s = None

    def stop(self) -> None:
        self._intent = "stopped"
        self._buffering = False
        self._to_null()

    def seek(self, position_s: int) -> bool:
        """Seek to ``position_s``. Returns False when GStreamer refuses (e.g. a
        non-seekable HTTP source that serves no byte ranges), so the caller can
        surface it instead of leaving the bar parked at a position that never
        took.

        A pipeline that has not played since its load (loaded paused, paused or
        buffering during preroll) is never seeked directly: a FLUSH seek on a
        never-started pipewiresink pipeline blocks forever and freezes the UI.
        The target is held and applied when it first reaches PLAYING (which is
        reported as the position meanwhile) and True is returned."""
        position_s = max(0, int(position_s))
        self._note_started()
        if self._sink is not None and not self._started and self._intent != "stopped":
            self._pending_seek_s = position_s
            return True
        self._pending_seek_s = None
        return self._seek_now(position_s)

    def _seek_now(self, position_s: int) -> bool:
        if self._plan.download and not self._downloaded_past(position_s):
            # The target isn't fetched yet, so the download buffer would have to
            # re-request the file from there -- and if the server answers that
            # with 403 (an expired URL/token) or ignores Range, GStreamer's flush
            # seek never returns, freezing the UI (and the teardown after it).
            # The queue plan seeks with an ordinary Range request that fails
            # fast into the normal error -> toast -> next-track path.
            return self._reload_with_queue_at(position_s)
        ok = self._playbin.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            position_s * Gst.SECOND,
        )
        if not ok:
            log.warning("local seek to %ss refused (stream not seekable)", position_s)
        return bool(ok)

    def _downloaded_past(self, position_s: int) -> bool:
        """Whether a progressive download has already fetched everything up to
        ``position_s`` (so a seek there never reaches the network). Conservative:
        any doubt (unknown duration, a failed query) says no."""
        try:
            ok, duration = self._playbin.query_duration(Gst.Format.TIME)
            query = Gst.Query.new_buffering(Gst.Format.PERCENT)
            if not ok or duration <= 0 or not self._playbin.query(query):
                return False
            _fmt, _start, stop, _total = query.parse_buffering_range()
        except Exception:  # noqa: BLE001
            return False
        fetched = stop / _PERCENT_MAX                       # fraction of the file on disk
        if fetched >= 0.999:
            return True
        needed = position_s * Gst.SECOND / duration         # time fraction ~ byte fraction (VBR slack)
        return fetched >= needed * 1.25 + 0.02

    def _reload_with_queue_at(self, position_s: int) -> bool:
        if not self._url:
            return False
        log.info("seek to %ss is past the download: switching to the streaming buffer", position_s)
        self.load_and_play(self._url, self._headers, paused=self._intent == "paused",
                           start_s=position_s, mime=self._mime, quality=self._quality,
                           plan=_STREAM_BUFFERING)
        return True

    def _apply_pending_seek(self) -> None:
        """The pipeline just reached PLAYING: do the seek that was deferred."""
        target = self._pending_seek_s
        if target is None:
            return
        # The bus message can be stale (a torn-down pipeline's PLAYING): only
        # seek a pipeline that really is playing now.
        if self._playbin.get_state(0)[1] != Gst.State.PLAYING:
            return
        self._pending_seek_s = None
        self._seek_now(target)

    def set_volume(self, level: int) -> None:
        if self._bit_perfect:
            return  # the level belongs to the DAC/amp; software volume stays at 100%
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
        if self._pending_seek_s is not None:
            ok_pos, pos = True, self._pending_seek_s * Gst.SECOND  # the deferred target
        ok_dur, dur = self._playbin.query_duration(Gst.Format.TIME)
        volume = int(round(self._playbin.get_property("volume") * 100))
        return LocalStatus(
            state=state,
            position_s=pos // Gst.SECOND if ok_pos and pos >= 0 else None,
            duration_s=dur // Gst.SECOND if ok_dur and dur >= 0 else None,
            volume=volume,
        )
