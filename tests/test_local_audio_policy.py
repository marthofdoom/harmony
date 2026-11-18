"""The local player's buffering policy and bit-perfect output (desktop).

* ``buffering_plan`` picks download / look-ahead queue / nothing by source type,
  and the player applies it to playbin (flags, buffer sizes, buffer elements).
* Bit-perfect pins the app volume at 100% (the bar's slider, MPRIS and the media
  keys all go through ``AppState.playback_set_volume``), remembers and restores
  the user's level, and persists in the settings.
* A real playbin fetches an HTTP track through a progressive-download temp file
  in the app's cache dir and cleans it up.

``scripts/e2e_buffering.py`` / ``scripts/e2e_bit_perfect.py`` drive the same
paths against a stalling HTTP server and a real PipeWire daemon.
"""

from __future__ import annotations

import http.server
import threading
import wave
from pathlib import Path

import pytest

pytest.importorskip("gi")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
except (ValueError, ImportError):  # pragma: no cover - no GStreamer typelib
    pytest.skip("GStreamer typelib unavailable", allow_module_level=True)

from gi.repository import GObject  # noqa: E402

from harmony import config as config_module  # noqa: E402
from harmony.audio.bitperfect import OFF, PENDING, VERIFIED, WARNING, AudioFormat  # noqa: E402
from harmony.audio.pipewire import STREAM_NODE_NAME  # noqa: E402
from harmony.models import Service, Track  # noqa: E402
from harmony.ui import local_player as lp_mod  # noqa: E402
from harmony.ui.local_player import (  # noqa: E402
    BufferingPlan,
    LocalPlayer,
    buffering_plan,
    stream_buffer_dir,
    sweep_stream_buffers,
)
from harmony.ui.now_playing_bar import NowPlayingBar  # noqa: E402
from harmony.ui.state import LOCAL_HOST, AppState  # noqa: E402

# -- buffering plan by source type --------------------------------------------------


@pytest.mark.parametrize(("url", "mode"), [
    ("file:///music/a.flac", "none"),
    ("/music/a.flac", "none"),
    ("http://192.168.1.10:8080/stream/abc123", "download"),               # a peer's Library
    ("https://streaming-qobuz-std.akamaized.net/file?uid=1", "download"),  # Qobuz FLAC
    ("https://rr3---sn-abc.googlevideo.com/videoplayback?id=1", "stream"),  # YouTube
    ("https://music.youtube.com/x", "stream"),
])
def test_buffering_plan_by_source(url: str, mode: str) -> None:
    assert buffering_plan(url).mode == mode


def test_download_plan_starts_after_a_second_or_two_of_audio_not_megabytes() -> None:
    """First audio = start window / network speed, so the window is ~2 s of the
    track's own bitrate (a 4 MiB one cost 4-6 s at 1 MB/s)."""
    from harmony.ui.local_player import start_window_bytes

    plan = buffering_plan("http://peer/stream/t")
    assert plan.download and plan.window_bytes <= 512 * 1024
    kib = 1024
    assert start_window_bytes(None, {"bitrate": 320}) == 80_000                   # 2 s of 320 kbps
    assert start_window_bytes(None, {"bitrate": 96}) == 64 * kib                  # floor
    assert start_window_bytes("audio/mpeg") == 80_000
    cd = {"lossless": True, "sample_rate": 44100, "bit_depth": 16, "channels": 2}
    hi192 = {"lossless": True, "sample_rate": 192000, "bit_depth": 24, "channels": 2}
    assert 64 * kib <= start_window_bytes("audio/flac", cd) < start_window_bytes("audio/flac", hi192)
    assert start_window_bytes("audio/flac", hi192) == 1024 * kib                    # capped at 1 MiB
    assert start_window_bytes(None, None) == 384 * kib                              # unknown
    big = buffering_plan("http://peer/stream/t", "audio/flac", hi192)
    assert big.window_bytes == 1024 * kib


def test_stream_plan_holds_tens_of_seconds_sized_for_hires_flac() -> None:
    plan = buffering_plan("https://x.googlevideo.com/v")
    assert 20 <= plan.stream_duration_s <= 30
    assert plan.stream_size_bytes >= 20 * 1024 * 1024
    assert 0 < plan.stream_low < plan.stream_high < 0.5     # starts early, rebuffers late


class FakePlaybin:
    def __init__(self) -> None:
        self.props: dict[str, object] = {"volume": 1.0, "flags": 0x617, "mute": False}
        self.signals: list[str] = []

    def set_property(self, name, value) -> None:
        self.props[name] = value

    def get_property(self, name):
        return self.props[name]

    def set_state(self, _st) -> None:
        pass

    def get_state(self, _t):
        return (Gst.StateChangeReturn.SUCCESS, Gst.State.NULL, Gst.State.VOID_PENDING)

    def query_position(self, _fmt):
        return True, 30 * Gst.SECOND


class FakeSink:
    def __init__(self) -> None:
        self.props: dict[str, object] = {}

    def set_property(self, name, value) -> None:
        self.props[name] = value

    def get_state(self, _t):
        return (Gst.StateChangeReturn.SUCCESS, Gst.State.NULL, Gst.State.VOID_PENDING)

    def get_static_pad(self, _n):
        return None


@pytest.fixture
def player() -> LocalPlayer:
    lp_mod._ensure_gst()
    p = LocalPlayer.__new__(LocalPlayer)
    p._on_eos = p._on_error = p._on_change = None
    p._intent, p._buffering, p._started = "stopped", False, False
    p._pending_seek_s, p._headers, p._logged_format = None, {}, False
    p._sink = FakeSink()
    p._playbin = FakePlaybin()
    return p


def test_download_source_sets_the_download_flag_and_buffer_sizes(player: LocalPlayer) -> None:
    player.load_and_play("http://peer/stream/t")
    assert player._playbin.props["flags"] & lp_mod._FLAG_DOWNLOAD
    assert player._playbin.props["flags"] & lp_mod._FLAG_SOFT_VOLUME  # the rest of the defaults stay
    assert player._playbin.props["buffer-duration"] > 0


def test_each_load_resets_the_flag_for_its_own_source(player: LocalPlayer) -> None:
    player.load_and_play("http://peer/stream/t")
    player.load_and_play("https://x.googlevideo.com/v")
    assert not player._playbin.props["flags"] & lp_mod._FLAG_DOWNLOAD     # queue, not a download
    assert player._playbin.props["buffer-duration"] == 30 * 1_000_000_000
    player.load_and_play("file:///a.flac")
    assert not player._playbin.props["flags"] & lp_mod._FLAG_DOWNLOAD
    assert player._playbin.props["buffer-duration"] == -1                 # playbin's default


class FakeElement:
    def __init__(self, factory: str) -> None:
        self._factory = factory
        self.props: dict[str, object] = {}

    def get_factory(self):
        factory = self

        class F:
            def get_name(self_inner) -> str:  # noqa: N805
                return factory._factory
        return F()

    def set_property(self, name, value) -> None:
        self.props[name] = value


def test_download_buffer_is_pointed_at_the_cache_dir_and_given_a_short_start_window(
        player: LocalPlayer) -> None:
    player.load_and_play("http://peer/stream/t")
    el = FakeElement("downloadbuffer")
    player._on_element_added(None, None, el)
    plan = player._plan
    assert Path(el.props["temp-template"]).parent == stream_buffer_dir()
    assert str(stream_buffer_dir()).startswith(str(config_module.cache_dir()))
    assert el.props["temp-template"].endswith("dl-XXXXXX")
    assert el.props["temp-remove"] is True
    assert el.props["max-size-time"] == plan.start_window_s * 1_000_000_000   # not playbin's 30 s
    assert el.props["max-size-bytes"] == plan.window_bytes
    assert stream_buffer_dir().is_dir()


def test_queue2_gets_watermarks_for_every_network_source(player: LocalPlayer) -> None:
    player.load_and_play("https://x.googlevideo.com/v")
    q = FakeElement("queue2")
    player._on_element_added(None, None, q)
    assert q.props["high-watermark"] == player._plan.stream_high
    assert q.props["low-watermark"] == player._plan.stream_low
    player.load_and_play("http://peer/stream/t")
    other = FakeElement("queue2")
    player._on_element_added(None, None, other)
    # a download source the server serves without a length falls back to queue2:
    # it must not wait for 99 % of a 30 s / 32 MiB queue either
    assert other.props["high-watermark"] == player._plan.stream_high
    player.load_and_play("file:///a.flac")
    local = FakeElement("queue2")
    player._on_element_added(None, None, local)
    assert local.props == {}


def test_audioconvert_dither_is_only_disabled_in_bit_perfect(player: LocalPlayer) -> None:
    """audioconvert's default is TPDF dither: leave it alone unless bit-perfect."""
    el = FakeElement("audioconvert")
    player._on_element_added(None, None, el)
    assert el.props == {}
    player.set_bit_perfect(True)
    player._on_element_added(None, None, el)
    assert el.props == {"dithering": 0, "noise-shaping": 0}


def test_sweep_removes_only_leftover_download_files() -> None:
    directory = stream_buffer_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dl-AbC123").write_bytes(b"x")
    (directory / "dl-Zz9999").write_bytes(b"x")
    keep = directory / "notes.txt"
    keep.write_text("keep")
    assert sweep_stream_buffers() == 2
    assert not list(directory.glob("dl-*")) and keep.exists()


# -- the PipeWire stream + volume under bit-perfect ------------------------------------


def _structure(sink: FakeSink) -> dict[str, str]:
    st = sink.props["stream-properties"]
    return {st.nth_field_name(i): st.get_string(st.nth_field_name(i)) for i in range(st.n_fields())}


def test_stream_properties_in_normal_mode(player: LocalPlayer) -> None:
    player.load_and_play("http://peer/stream/t")
    props = _structure(player._sink)
    assert props["node.name"] == STREAM_NODE_NAME
    assert props["node.latency"]                      # headroom, not the tiny default
    assert "node.dont-remix" not in props and "node.lock-rate" not in props


def test_bit_perfect_stream_never_remixes_and_locks_the_rate(player: LocalPlayer) -> None:
    player.set_bit_perfect(True)
    player.load_and_play("http://peer/stream/t")
    props = _structure(player._sink)
    assert props["node.dont-remix"] == "true"
    assert props["node.lock-rate"] == "true"
    # resampling stays allowed (a track the device can't run still plays; the
    # indicator says so) and the rate is never forced past allowed-rates
    assert not any(k in props for k in ("resample.disable", "node.force-rate", "audio.rate"))


def test_bit_perfect_pins_volume_and_ignores_set_volume(player: LocalPlayer) -> None:
    player._playbin.props["volume"] = 0.4
    player.set_bit_perfect(True)
    assert player._playbin.props["volume"] == 1.0 and player._playbin.props["mute"] is False
    player.set_volume(30)
    assert player._playbin.props["volume"] == 1.0
    player.set_bit_perfect(False)
    player.set_volume(30)
    assert player._playbin.props["volume"] == 0.3


def test_toggling_while_playing_asks_the_caller_to_restart(player: LocalPlayer) -> None:
    """The player never reloads the (possibly expired) URL itself: it reports that a
    restart is needed and AppState replays through the normal, re-resolving path."""
    assert player.set_bit_perfect(True) is False                      # idle: nothing to restart
    player.set_bit_perfect(False)
    player.load_and_play("http://peer/stream/t")
    player._intent = "playing"
    assert player.set_bit_perfect(True) is True
    assert _structure(player._sink)["node.dont-remix"] == "true"      # new sink config armed
    assert player.set_bit_perfect(True) is False                      # no change: nothing to do
    player._intent = "stopped"
    assert player.set_bit_perfect(False) is False


def test_current_position_prefers_a_deferred_seek(player: LocalPlayer) -> None:
    assert player.current_position_s() == 30        # the pipeline's own position
    player._pending_seek_s = 95                     # never played yet: the seek waits
    assert player.current_position_s() == 95


class DownloadPlaybin(FakePlaybin):
    """A playbin mid-download: ``fetched`` of the file is on disk."""

    def __init__(self, fetched: float, duration_s: int = 200) -> None:
        super().__init__()
        self.fetched, self.duration_s = fetched, duration_s
        self.seeks: list[int] = []

    def query_duration(self, _fmt):
        return True, self.duration_s * Gst.SECOND

    def query(self, q) -> bool:
        q.set_buffering_range(Gst.Format.PERCENT, 0, int(self.fetched * 1_000_000), -1)
        return True

    def seek_simple(self, _fmt, _flags, pos) -> bool:
        self.seeks.append(pos // Gst.SECOND)
        return True


def test_seek_inside_the_downloaded_part_is_a_plain_seek(player: LocalPlayer) -> None:
    player._playbin = DownloadPlaybin(fetched=0.6)
    player.load_and_play("http://peer/stream/t")
    player._started, player._intent = True, "playing"
    player._playbin.seeks.clear()
    assert player.seek(60) is True                  # 30 % of the track, 60 % fetched
    assert player._playbin.seeks == [60]
    assert player._plan.download


def test_seek_past_the_download_never_issues_a_blocking_flush_seek(player: LocalPlayer) -> None:
    """The downloadbuffer would have to re-request the file; with a 403/no-Range
    server that flush seek never returns and freezes the UI. The track is reloaded
    with the queue plan at the target instead (a plain Range request that fails fast)."""
    player._playbin = DownloadPlaybin(fetched=0.2)
    player.load_and_play("http://peer/stream/t")
    player._started, player._intent = True, "playing"
    player._playbin.seeks.clear()
    assert player.seek(150) is True                 # 75 % of the track, 20 % fetched
    assert player._playbin.seeks == []              # no seek on the download pipeline
    assert not player._plan.download and player._plan.mode == "stream"
    assert player._pending_seek_s == 150            # applied once the new pipeline plays


def test_seek_with_unknown_download_state_takes_the_safe_path(player: LocalPlayer) -> None:
    class Blind(DownloadPlaybin):
        def query(self, q) -> bool:
            return False

    player._playbin = Blind(fetched=1.0)
    player.load_and_play("http://peer/stream/t")
    player._started, player._intent = True, "playing"
    assert player.seek(10) is True
    assert player._playbin.seeks == [] and player._plan.mode == "stream"


def test_fully_downloaded_track_seeks_anywhere(player: LocalPlayer) -> None:
    player._playbin = DownloadPlaybin(fetched=1.0)
    player.load_and_play("http://peer/stream/t")
    player._started, player._intent = True, "playing"
    player._playbin.seeks.clear()
    player.seek(199)
    assert player._playbin.seeks == [199]


def test_resuming_mid_track_starts_on_the_queue_plan(player: LocalPlayer) -> None:
    player.load_and_play("http://peer/stream/t", start_s=120)
    assert player._plan.mode == "stream"
    player.load_and_play("http://peer/stream/t")
    assert player._plan.download


# -- AppState: persistence, volume lock, MPRIS ----------------------------------------


@pytest.fixture
def state(monkeypatch, tmp_path) -> AppState:
    monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")
    obj = AppState.__new__(AppState)
    GObject.Object.__init__(obj)
    obj.settings = config_module.Settings.load()
    obj._device_session = None
    obj._init_playback()
    obj._ensure_poller = lambda: None
    return obj


class FakeLocal:
    def __init__(self) -> None:
        self.volume = 70
        self.bit_perfect = False
        self.set_volumes: list[int] = []

    def set_bit_perfect(self, on: bool) -> None:
        self.bit_perfect = on
        if on:
            self.volume = 100

    def set_volume(self, level: int) -> None:
        self.set_volumes.append(level)
        if not self.bit_perfect:
            self.volume = level

    def status(self):
        return lp_mod.LocalStatus("playing", 10, 200, self.volume)

    def decoded_format(self):
        return AudioFormat(96000, "S24_32LE", 2)

    def sink_format(self):
        return AudioFormat(96000, "S24_32LE", 2)


def _playing_locally(state: AppState, volume: int = 70) -> FakeLocal:
    fake = FakeLocal()
    fake.volume = volume
    state._local_player = fake
    pb = state.playback
    pb.active_host = LOCAL_HOST
    pb.track = Track(id="t", title="Song", service=Service.YTMUSIC, artists=["A"], duration_s=200)
    pb.state, pb.volume, pb.volume_supported = "playing", volume, True
    return fake


def test_bit_perfect_defaults_off_and_persists(state: AppState, tmp_path) -> None:
    assert state.settings.bit_perfect is False
    state.set_bit_perfect(True)
    assert config_module.Settings.load().bit_perfect is True
    state.set_bit_perfect(False)
    assert config_module.Settings.load().bit_perfect is False


def test_turning_it_on_pins_volume_and_off_restores_the_users_level(state: AppState) -> None:
    fake = _playing_locally(state, volume=70)
    state.set_bit_perfect(True)
    assert fake.bit_perfect and state.playback.volume == 100
    assert state.settings.bit_perfect_saved_volume == 70
    assert state.bit_perfect_locks_volume()
    state.set_bit_perfect(False)
    assert not fake.bit_perfect and fake.volume == 70 and state.playback.volume == 70
    assert not state.bit_perfect_locks_volume()


def test_unknown_level_keeps_the_saved_volume(state: AppState) -> None:
    state.settings.bit_perfect_saved_volume = 35        # remembered from an earlier session
    state.set_bit_perfect(True)                         # nothing has played here: level unknown
    assert state.settings.bit_perfect_saved_volume == 35
    state.set_bit_perfect(False)
    assert state._pending_local_volume == 35            # applied when the player is first created


def test_a_saved_volume_of_zero_is_a_real_level(state: AppState) -> None:
    fake = _playing_locally(state, volume=0)
    state.set_bit_perfect(True)
    assert state.settings.bit_perfect_saved_volume == 0          # not "or 100"
    state.set_bit_perfect(False)
    assert fake.volume == 0 and state.playback.volume == 0
    state.settings.bit_perfect_saved_volume = 0
    state._local_player = None
    state.settings.bit_perfect = True
    state.set_bit_perfect(False)
    assert state._pending_local_volume == 0


def test_toggling_while_playing_replays_through_the_normal_path(state: AppState, monkeypatch) -> None:
    """Fresh stream URL (the held one may have expired), same position, paused stays paused."""
    fake = _playing_locally(state, volume=70)
    fake.current_position_s = lambda: 95
    fake.intent = "playing"
    fake.set_bit_perfect = lambda on: setattr(fake, "bit_perfect", on) or True
    state.queue.load([state.playback.track, Track(id="n", title="N", service=Service.YTMUSIC, artists=["A"])], 0)
    started: list[int] = []
    monkeypatch.setattr(state, "_start_local", lambda i: (started.append(i), setattr(state, "_play_gen", 9)))
    state.playback.state = "paused"
    state.set_bit_perfect(True)
    assert started == [0]
    assert state._local_start_at == (9, 95) and state._pause_after_resolve is True


def test_the_probe_backs_off_when_the_stream_never_links(state: AppState, monkeypatch) -> None:
    """e.g. the stream only feeds a recorder: don't hit pw-dump every second forever."""
    from harmony.audio.pipewire import StreamReport

    _playing_locally(state)
    state.settings.bit_perfect = True
    dones: list = []
    monkeypatch.setattr("harmony.ui.state.run_async", lambda w, d, f: dones.append(d))
    state._bp_tick()
    delays = []
    for _ in range(6):
        state._bp_next = 0.0
        state._bp_tick()
        dones.pop()(StreamReport())                                   # not linked
        delays.append(round(state._bp_next - __import__("time").monotonic()))
    assert delays[:3] == [1, 1, 1] and delays[3:] == [5, 5, 5]


def test_volume_requests_are_ignored_while_bit_perfect_is_on(state: AppState) -> None:
    """The bar's slider, MPRIS ``Volume`` and media keys all end up here."""
    fake = _playing_locally(state, volume=70)
    state.set_bit_perfect(True)
    fake.set_volumes.clear()
    state.playback_set_volume(20)
    assert state.playback.volume == 100 and fake.set_volumes == []
    assert fake.volume == 100


def test_volume_works_as_before_when_bit_perfect_is_off(state: AppState) -> None:
    fake = _playing_locally(state, volume=70)
    state.playback_set_volume(20)
    assert state.playback.volume == 20 and fake.set_volumes == [20]


def test_a_device_target_keeps_its_own_volume_under_bit_perfect(state: AppState, monkeypatch) -> None:
    state.settings.bit_perfect = True
    state.playback.active_host = "192.168.1.5"
    sent: list = []
    monkeypatch.setattr(state, "_remote_control", lambda *a, **k: sent.append(a))
    state.playback_set_volume(30)
    assert sent == [("volume", 30)] and state.playback.volume == 30
    assert not state.bit_perfect_locks_volume()


def test_status_off_pending_then_verified_and_amber(state: AppState) -> None:
    from harmony.audio.pipewire import StreamReport

    _playing_locally(state, volume=100)
    assert state.bit_perfect_status().level == OFF
    state.settings.bit_perfect = True
    assert state.bit_perfect_status().level == PENDING                   # PipeWire not read yet
    state._bp_key = (state.playback.track_key(), AudioFormat(96000, "S24_32LE", 2))
    state._bp_probed = True
    state._bp_report = StreamReport(linked=True, stream_rate=96000, stream_format="S24_32LE",
                                    stream_channels=2, sink_rate=96000, sink_format="S32LE",
                                    sink_channels=2, sink_description="DAC", sink_kind="alsa", dont_remix=True)
    assert state.bit_perfect_status().level == VERIFIED
    state._bp_report = StreamReport(linked=True, stream_rate=96000, stream_format="S24_32LE",
                                    stream_channels=2, sink_rate=48000, sink_format="S32LE",
                                    sink_channels=2, sink_description="DAC", sink_kind="alsa", allowed_rates=(48000,))
    s = state.bit_perfect_status()
    assert s.level == WARNING and s.label == "Resampled 96→48 kHz"
    # a new track must not be judged against the previous track's reading
    state.playback.track = Track(id="u", title="Next", service=Service.YTMUSIC, artists=["A"])
    assert state.bit_perfect_status().level == PENDING


def test_probe_is_rescheduled_for_each_new_track(state: AppState, monkeypatch) -> None:
    _playing_locally(state)
    state.settings.bit_perfect = True
    from harmony.audio.pipewire import StreamReport

    ran: list[int] = []
    dones: list = []
    monkeypatch.setattr("harmony.ui.state.run_async",
                        lambda work, done, failed: (ran.append(1), dones.append(done)))
    state._bp_tick()
    assert ran == []                     # waits a moment for the stream to connect
    state._bp_next = 0.0
    state._bp_tick()
    assert ran == [1]
    state._bp_tick()
    assert ran == [1]                    # one read in flight at a time
    dones[0](StreamReport(linked=True, stream_rate=96000, sink_rate=96000))
    assert state._bp_probed
    state._bp_tick()
    assert ran == [1]                    # then every few seconds, not every tick
    state.playback.track = Track(id="u", title="Next", service=Service.YTMUSIC, artists=["A"])
    state._bp_tick()
    assert state._bp_report is None and not state._bp_probed    # new track: the old reading is dropped


# -- the bar --------------------------------------------------------------------------


def test_bar_volume_slider_is_locked_with_a_dac_tooltip(state: AppState) -> None:
    _playing_locally(state, volume=70)
    bar = NowPlayingBar(state)
    bar._render()
    assert bar._volume.get_sensitive() and bar._vol_box.get_tooltip_text() is None
    state.set_bit_perfect(True)
    bar._render()
    assert not bar._volume.get_sensitive()
    assert "DAC/amp" in bar._vol_box.get_tooltip_text()
    assert bar._volume.get_value() == 100            # sits at the top: volume is 100%
    state.set_bit_perfect(False)
    bar._render()
    assert bar._volume.get_sensitive() and bar._vol_box.get_tooltip_text() is None


def test_now_playing_page_info_shows_the_same_state_as_the_bar(state: AppState) -> None:
    """The page's info block is the same widget as the bar's: one source of truth."""
    from harmony.ui.np_info import NowPlayingInfo

    _playing_locally(state, volume=100)
    state.settings.bit_perfect = True
    state.playback.quality = {"codec": "FLAC", "lossless": True, "tier": "hires", "label": "FLAC"}
    bar_info, page_info = NowPlayingInfo(state), NowPlayingInfo(state, large=True, show_album=True)
    bar_info.update()
    page_info.update()
    assert page_info.bp.get_visible() and bar_info.bp.get_visible()
    assert page_info.bp_label.get_label() == bar_info.bp_label.get_label()
    assert page_info.bp.get_tooltip_text() == bar_info.bp.get_tooltip_text()


def test_the_pill_opens_settings_to_turn_on_but_turns_off_directly(state: AppState, monkeypatch) -> None:
    """Enabling jumps the volume to 100%: not on a stray click."""
    from harmony.ui import widgets
    from harmony.ui.np_info import NowPlayingInfo

    opened: list[str] = []
    monkeypatch.setattr(widgets, "open_preferences", lambda _w, page="accounts": opened.append(page))
    fake = _playing_locally(state, volume=20)
    info = NowPlayingInfo(state)
    info.bp.emit("clicked")
    assert opened == ["audio"] and not state.bit_perfect and fake.volume == 20
    state.set_bit_perfect(True)
    assert fake.bit_perfect
    info.bp.emit("clicked")
    assert opened == ["audio"] and not state.bit_perfect and fake.volume == 20   # restored


def test_bit_perfect_badge_follows_the_status_on_bar_and_page_info(state: AppState) -> None:
    _playing_locally(state, volume=100)
    bar = NowPlayingBar(state)
    state.playback.quality = {"codec": "FLAC", "lossless": True, "tier": "hires", "label": "FLAC"}
    bar._render()
    badge = bar._info.bp
    assert badge.get_visible()                                        # local output: shown, grey when off
    assert not badge.has_css_class("bp-verified") and not badge.has_css_class("bp-warning")
    state.settings.bit_perfect = True
    state._bp_probed = True
    state._bp_key = (state.playback.track_key(), AudioFormat(96000, "S24_32LE", 2))
    from harmony.audio.pipewire import StreamReport
    state._bp_report = StreamReport(linked=True, stream_rate=96000, stream_format="S24_32LE", stream_channels=2,
                                    sink_rate=48000, sink_format="S32LE", sink_channels=2,
                                    sink_kind="alsa", allowed_rates=(48000,))
    bar._render()
    assert badge.has_css_class("bp-warning")
    assert "96→48" in bar._info.bp_label.get_label()
    assert "doesn't allow 96 kHz" in badge.get_tooltip_text()
    state._bp_report = StreamReport(linked=True, stream_rate=96000, stream_format="S24_32LE", stream_channels=2,
                                    sink_rate=96000, sink_format="S32LE", sink_channels=2,
                                    sink_kind="alsa")
    bar._render()
    assert badge.has_css_class("bp-verified") and not badge.has_css_class("bp-warning")
    state.playback.active_host = "192.168.1.5"                         # casting: not our output
    bar._render()
    assert not badge.get_visible()


# -- a real playbin through a real HTTP server ---------------------------------------


def _wav(path: Path, seconds: int = 20, rate: int = 44100) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x10" * 2 * rate * seconds)


@pytest.fixture
def http_server(tmp_path):
    _wav(tmp_path / "t.wav")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(tmp_path), **k)

        def log_message(self, *a):
            pass

        def send_head(self):  # a minimal Range-capable file server
            path = Path(self.translate_path(self.path))
            data = path.read_bytes()
            rng = self.headers.get("Range")
            start, code = 0, 200
            if rng:
                start, code = int(rng.split("=")[1].split("-")[0]), 206
            body = data[start:]
            self.send_response(code)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(body)))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.end_headers()
            self.wfile.write(body) if self.command == "GET" else None
            return None

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/t.wav"
    srv.shutdown()


def test_real_playbin_downloads_ahead_into_the_cache_dir_and_cleans_up(http_server: str) -> None:
    """The progressive-download buffer really is used for an HTTP source, its temp
    file lands in the app's cache dir (not /tmp), and it is deleted afterwards."""
    from gi.repository import GLib

    lp_mod._ensure_gst()
    lp = LocalPlayer()
    lp._playbin.set_property("audio-sink", Gst.ElementFactory.make("fakesink", None))
    seen: list[str] = []
    elements: list[str] = []
    lp._playbin.connect("deep-element-added", lambda _p, _b, el: elements.append(el.get_factory().get_name()))
    try:
        lp.load_and_play(http_server)
        ctx = GLib.MainContext.default()
        import time
        end = time.monotonic() + 8
        while time.monotonic() < end:
            while ctx.iteration(False):
                pass
            seen = [p.name for p in stream_buffer_dir().glob("dl-*")]
            if seen and lp.status().state == "playing":
                break
            time.sleep(0.05)
        assert "downloadbuffer" in elements
        assert seen, "no progressive-download temp file in the cache dir"
    finally:
        lp.stop()
    assert not list(stream_buffer_dir().glob("dl-*")), "temp file left behind"


def test_buffering_plan_dataclass_is_frozen() -> None:
    with pytest.raises(AttributeError):
        BufferingPlan().mode = "x"  # type: ignore[misc]
