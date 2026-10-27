"""Transport-state regressions: the play/pause button showed "paused" (and Stop was
dead) while audio played.

Root cause: ``LocalPlayer.status()`` mapped GStreamer's *current* state, so a poll
landing in an async transition (preroll / buffering / seek: current READY or
PAUSED, PLAYING merely pending) read "stopped"; the model took it, and since the
poller only synced while the model said playing/paused it never recovered.

The player now reports the user's *intent* (loading while the pipeline catches
up), handles BUFFERING itself, and the model/devices hold an optimistic
play/pause/stop for a short settle window. A fake playbin stands in for
GStreamer's lag; ``scripts/e2e_playback_state.py`` drives the real thing.
"""

from __future__ import annotations

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
from harmony.models import Service, StreamSource, Track  # noqa: E402
from harmony.ui import local_player as lp_mod  # noqa: E402
from harmony.ui import state as state_mod  # noqa: E402
from harmony.ui.local_player import LocalPlayer, LocalStatus  # noqa: E402
from harmony.ui.now_playing_bar import NowPlayingBar  # noqa: E402
from harmony.ui.state import LOCAL_HOST, AppState  # noqa: E402
from harmony.web.api import track_to_dict  # noqa: E402

# -- LocalPlayer with a fake playbin ------------------------------------------


class FakePlaybin:
    """Async like the real thing: set_state records the *target*; the current
    state only changes when the test calls ``arrive()``."""

    def __init__(self) -> None:
        self.current = Gst.State.NULL
        self.pending = Gst.State.VOID_PENDING
        self.calls: list[Gst.State] = []
        self.props: dict[str, object] = {"volume": 0.5}

    def set_state(self, st) -> None:
        self.calls.append(st)
        self.pending = st if st != self.current else Gst.State.VOID_PENDING
        if st == Gst.State.NULL:
            self.current, self.pending = st, Gst.State.VOID_PENDING

    def arrive(self) -> None:
        if self.pending != Gst.State.VOID_PENDING:
            self.current, self.pending = self.pending, Gst.State.VOID_PENDING

    def get_state(self, _timeout):
        return (Gst.StateChangeReturn.ASYNC if self.pending != Gst.State.VOID_PENDING
                else Gst.StateChangeReturn.SUCCESS, self.current, self.pending)

    def set_property(self, name, value) -> None:
        self.props[name] = value

    def get_property(self, name):
        return self.props[name]

    def query_position(self, _fmt):
        return True, 3 * Gst.SECOND

    def query_duration(self, _fmt):
        return True, 200 * Gst.SECOND

    def seek_simple(self, *_a) -> bool:
        return True


class FakeBuffering:
    def __init__(self, percent: int, mode=None) -> None:
        self._p = percent
        self._mode = Gst.BufferingMode.STREAM if mode is None else mode

    def parse_buffering(self) -> int:
        return self._p

    def parse_buffering_stats(self):
        class Stats:
            mode = self._mode
        return Stats()


@pytest.fixture
def player() -> LocalPlayer:
    lp_mod._ensure_gst()
    p = LocalPlayer.__new__(LocalPlayer)
    p._on_eos = p._on_error = None
    p._on_change = None
    p._intent = "stopped"
    p._buffering = False
    p._never_played = False
    p._headers = {}
    p._sink = None
    p._logged_format = False
    p._playbin = FakePlaybin()
    return p


def test_status_follows_intent_through_preroll(player: LocalPlayer) -> None:
    """The bug: READY (PLAYING pending) read as "stopped"."""
    player.load_and_play("http://x/y.flac")
    assert player._playbin.get_state(0)[1] == Gst.State.NULL  # nothing arrived yet
    assert player.status().state == "loading"
    player._playbin.current = Gst.State.READY  # mid-preroll, PLAYING pending
    assert player.status().state == "loading"
    player._playbin.current = Gst.State.PAUSED
    assert player.status().state == "loading"
    player._playbin.arrive()
    player._playbin.current = Gst.State.PLAYING
    assert player.status().state == "playing"


def test_pause_and_stop_report_intent_immediately(player: LocalPlayer) -> None:
    player.load_and_play("u")
    player._playbin.current = Gst.State.PLAYING
    player.pause()
    assert player._playbin.current == Gst.State.PLAYING  # pipeline hasn't caught up
    assert player.status().state == "paused"
    player.resume()
    assert player.status().state == "playing"
    player.stop()
    assert player.status().state == "stopped"
    assert player.intent == "stopped"


def test_load_paused_starts_paused(player: LocalPlayer) -> None:
    player.load_and_play("u", paused=True)
    assert player._playbin.calls[-1] == Gst.State.PAUSED
    assert player.status().state == "paused"


def test_stopping_a_never_started_paused_load_starts_it_muted_first(player: LocalPlayer) -> None:
    """A track loaded paused (Pause pressed during its resolve) leaves pipewiresink
    prerolled but never started; taking that down to NULL deadlocks the main
    thread (Stop froze the UI). It is started muted first, then shut down."""
    player.load_and_play("u", paused=True)
    player._playbin.current = Gst.State.PAUSED  # prerolled
    player._playbin.calls.clear()
    player.stop()
    assert player._playbin.calls == [Gst.State.PLAYING, Gst.State.NULL]
    assert player._playbin.props["mute"] is False  # never left muted
    # a pipeline that did play (or was resumed) goes straight down
    player.load_and_play("u")
    player._playbin.current = Gst.State.PLAYING
    player._playbin.calls.clear()
    player.stop()
    assert player._playbin.calls == [Gst.State.NULL]
    player.load_and_play("u", paused=True)
    player.resume()
    player._playbin.current = Gst.State.PLAYING
    player._playbin.calls.clear()
    player.pause()
    player.stop()
    assert player._playbin.calls == [Gst.State.PAUSED, Gst.State.NULL]


def test_buffering_holds_pipeline_but_keeps_intent(player: LocalPlayer) -> None:
    player.load_and_play("u")
    player._playbin.current = Gst.State.PLAYING
    player._playbin.calls.clear()
    player._handle_buffering(None, FakeBuffering(30))
    assert player._playbin.calls == [Gst.State.PAUSED]
    player._playbin.current = Gst.State.PAUSED
    assert player.status().state == "loading"  # still wants to play: no flicker to "paused"
    player._handle_buffering(None, FakeBuffering(60))
    assert player._playbin.calls == [Gst.State.PAUSED]  # no extra transitions mid-fill
    player._handle_buffering(None, FakeBuffering(100))
    assert player._playbin.calls[-1] == Gst.State.PLAYING


def test_user_pause_during_buffering_is_not_undone(player: LocalPlayer) -> None:
    player.load_and_play("u")
    player._handle_buffering(None, FakeBuffering(10))
    player.pause()
    player._playbin.calls.clear()
    player._handle_buffering(None, FakeBuffering(100))
    assert player._playbin.calls == []  # stays paused
    assert player.status().state == "paused"


def test_resume_during_buffering_waits_for_the_buffer(player: LocalPlayer) -> None:
    player.load_and_play("u")
    player._handle_buffering(None, FakeBuffering(10))
    player.pause()
    player.resume()
    assert player._playbin.calls[-1] == Gst.State.PAUSED  # not PLAYING yet
    assert player.status().state == "loading"
    player._handle_buffering(None, FakeBuffering(100))
    assert player._playbin.calls[-1] == Gst.State.PLAYING


def test_buffering_ignored_when_stopped_or_live(player: LocalPlayer) -> None:
    player._handle_buffering(None, FakeBuffering(5))
    assert player._playbin.calls == [] and not player._buffering
    player.load_and_play("u")
    player._playbin.calls.clear()
    player._handle_buffering(None, FakeBuffering(5, Gst.BufferingMode.LIVE))
    assert player._playbin.calls == [] and not player._buffering


def test_error_resets_intent(player: LocalPlayer) -> None:
    class Msg:
        def parse_error(self):
            return "boom", ""

    player.load_and_play("u")
    player._handle_error(None, Msg())
    assert player.status().state == "stopped"


def test_buffering_change_notifies(player: LocalPlayer) -> None:
    seen: list[int] = []
    player._on_change = lambda: seen.append(1)
    player.load_and_play("u")
    player._handle_buffering(None, FakeBuffering(10))
    player._handle_buffering(None, FakeBuffering(50))  # no change in flag
    player._handle_buffering(None, FakeBuffering(100))
    assert len(seen) == 2


# -- AppState + bar ------------------------------------------------------------


class FakeLocal:
    """Stands in for LocalPlayer inside AppState."""

    def __init__(self, state: str = "stopped") -> None:
        self.state = state
        self.calls: list[object] = []

    @property
    def intent(self) -> str:
        return {"loading": "playing"}.get(self.state, self.state)

    def status(self) -> LocalStatus:
        return LocalStatus(self.state, 7, 200, 50)

    def pause(self) -> None:
        self.calls.append("pause")
        self.state = "paused"

    def resume(self) -> None:
        self.calls.append("resume")
        self.state = "playing"

    def stop(self) -> None:
        self.calls.append("stop")
        self.state = "stopped"

    def load_and_play(self, url, headers=None, paused=False) -> None:
        self.calls.append(("load", url, paused))
        self.state = "paused" if paused else "loading"


def _track(i: int = 1) -> Track:
    return Track(id=f"t{i}", title=f"Song {i}", service=Service.YTMUSIC, artists=["A"],
                 duration_s=200)


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


def _local(state: AppState, pb_state: str, player_state: str) -> FakeLocal:
    fake = FakeLocal(player_state)
    state._local_player = fake
    state.queue.load([_track(1), _track(2)], 0)
    pb = state.playback
    pb.active_host, pb.track, pb.state = LOCAL_HOST, state.queue.current(), pb_state
    return fake


def test_poll_heals_a_stale_stopped_model(state: AppState) -> None:
    """The sticky bug: model "stopped" while the player plays was never revisited."""
    _local(state, "stopped", "playing")
    state._poll()
    assert state.playback.state == "playing"


def test_transient_player_load_never_reads_as_stopped(state: AppState) -> None:
    fake = _local(state, "loading", "loading")
    state._poll()
    assert state.playback.state == "loading"
    fake.state = "playing"
    state._on_local_changed()  # the bus callback promotes it without waiting for the poll
    assert state.playback.state == "playing"


def test_poll_ignored_while_stream_resolves(state: AppState) -> None:
    _local(state, "loading", "stopped")
    state._local_resolving = True
    state._poll()
    assert state.playback.state == "loading"


def test_stopped_stays_parked(state: AppState) -> None:
    _local(state, "stopped", "stopped")
    state.playback.position_s = 0
    state._poll()
    assert state.playback.state == "stopped"
    assert state.playback.position_s == 0


def test_toggle_on_stale_stopped_pauses_instead_of_restarting(state: AppState, monkeypatch) -> None:
    fake = _local(state, "stopped", "playing")
    jumped: list[int] = []
    monkeypatch.setattr(state, "playback_jump", jumped.append)
    state.playback_toggle_pause()
    assert jumped == []  # did not restart the track
    assert fake.calls == ["pause"]
    assert state.playback.state == "paused"


def test_stop_stops_the_player_even_if_model_is_stale(state: AppState) -> None:
    fake = _local(state, "stopped", "playing")
    assert state.can_stop() is True
    state.playback_stop()
    assert fake.calls == ["stop"]
    assert state.playback.state == "stopped"
    assert state.can_stop() is False


def test_can_stop_rules(state: AppState) -> None:
    pb = state.playback
    assert state.can_stop() is False  # nothing active
    _local(state, "stopped", "stopped")
    assert state.can_stop() is False  # confirmed stopped
    for st in ("playing", "paused", "loading", "unknown"):
        pb.state = st
        assert state.can_stop() is True, st
    pb.state = "stopped"
    state._local_player.state = "loading"  # model stale, audio about to play
    assert state.can_stop() is True
    pb.track = None
    assert state.can_stop() is False
    # a device: only the model's word counts
    pb.track, pb.active_host, pb.state = _track(), "wiim-1", "stopped"
    assert state.can_stop() is False
    pb.state = "unknown"
    assert state.can_stop() is True


def test_bar_stop_sensitivity_uses_can_stop(state: AppState) -> None:
    bar = NowPlayingBar(state)
    _local(state, "stopped", "playing")
    bar._render()
    assert bar._stop.get_sensitive() is True
    state._local_player.state = "stopped"
    bar._render()
    assert bar._stop.get_sensitive() is False
    state.playback.state = "unknown"
    bar._render()
    assert bar._stop.get_sensitive() is True


def _resolve_inline(monkeypatch) -> None:
    monkeypatch.setattr(state_mod, "run_async",
                        lambda work, done=None, failed=None, *a, **k: done(work()))


class _Prov:
    def resolve_stream(self, track_id, max_quality=False):
        return StreamSource(url=f"http://x/{track_id}", mime_type="audio/flac")


def test_start_local_goes_loading_then_player_decides(state: AppState, monkeypatch) -> None:
    _resolve_inline(monkeypatch)
    fake = FakeLocal("stopped")
    state._local_player = fake
    state.providers = {Service.YTMUSIC: _Prov()}
    monkeypatch.setattr(state, "_engine", lambda: (_ for _ in ()).throw(RuntimeError("no"))
                        )
    state.queue.load([_track(1)], 0)
    state._start_local(0)
    # the previous pipeline is silenced when the resolve begins, then the new one loads
    assert fake.calls == ["stop", ("load", "http://x/t1", False)]
    assert state._local_resolving is False
    assert state.playback.state == "loading"
    state._poll_tick = 0
    state.playback.active_host = LOCAL_HOST
    fake.state = "playing"
    state._poll()
    assert state.playback.state == "playing"


def test_pause_while_resolving_starts_paused(state: AppState, monkeypatch) -> None:
    """Pausing during the resolve window used to be lost: playback then began anyway."""
    captured: dict = {}
    monkeypatch.setattr(state_mod, "run_async",
                        lambda work, done=None, failed=None, *a, **k: captured.update(
                            work=work, done=done))
    fake = FakeLocal("stopped")
    state._local_player = fake
    state.providers = {Service.YTMUSIC: _Prov()}
    monkeypatch.setattr(state, "_engine", lambda: (_ for _ in ()).throw(RuntimeError("no")))
    state.queue.load([_track(1)], 0)
    state._start_local(0)
    assert state._local_resolving is True
    state.playback_toggle_pause()  # pause
    assert state.playback.state == "paused"
    captured["done"](captured["work"]())
    assert fake.calls[-1] == ("load", "http://x/t1", True)
    assert state.playback.state == "paused"


def _slow_resolve(state: AppState, monkeypatch, fake: FakeLocal) -> dict:
    captured: dict = {}
    monkeypatch.setattr(state_mod, "run_async",
                        lambda work, done=None, failed=None, *a, **k: captured.update(
                            work=work, done=done))
    state._local_player = fake
    state.providers = {Service.YTMUSIC: _Prov()}
    monkeypatch.setattr(state, "_engine", lambda: (_ for _ in ()).throw(RuntimeError("no")))
    state.queue.load([_track(1), _track(2)], 0)
    return captured


def test_transport_while_resolving_never_touches_the_old_pipeline(state: AppState, monkeypatch) -> None:
    """Play A, Stop, then pause/play while B resolves: the player still holds A's
    URI, so pausing/resuming it used to preroll and play A."""
    fake = FakeLocal("stopped")  # A was stopped (NULL pipeline, A's URI)
    captured = _slow_resolve(state, monkeypatch, fake)
    state._start_local(0)
    assert state._local_resolving is True
    n = len(fake.calls)
    state.playback_toggle_pause()  # pause
    state.playback_toggle_pause()  # play
    state.playback_toggle_pause()  # pause again
    assert fake.calls[n:] == []  # the old pipeline was not paused/resumed/prerolled
    assert fake.state == "stopped"
    assert state.playback.state == "paused"
    captured["done"](captured["work"]())
    assert fake.calls[-1] == ("load", "http://x/t1", True)  # last intent: paused


def test_next_while_playing_silences_the_old_track_during_the_resolve(state: AppState, monkeypatch) -> None:
    """Next while A plays: A must not keep playing (or be resumed by Play) while B resolves."""
    fake = FakeLocal("playing")
    captured = _slow_resolve(state, monkeypatch, fake)
    state._start_local(1)
    assert fake.calls == ["stop"] and fake.state == "stopped"
    state.playback_toggle_pause()   # pause during the resolve
    state.playback_toggle_pause()   # and play again
    assert fake.calls == ["stop"] and fake.state == "stopped"
    captured["done"](captured["work"]())
    assert fake.calls[-1] == ("load", "http://x/t2", False)


def test_new_track_and_output_switch_clear_the_transport_intent(state: AppState, monkeypatch) -> None:
    _device(state, "playing")
    monkeypatch.setattr(state, "_remote_control", lambda *a, **k: None)
    state.playback_toggle_pause()
    assert state._transport_intent == "paused"
    state._mark_now_playing("wiim-1", _track(2), state="loading")
    assert state._transport_intent is None
    state._note_transport("stopped")
    monkeypatch.setattr(state, "_halt_output", lambda *a, **k: None)
    state._switch_output(LOCAL_HOST)
    assert state._transport_intent is None


# -- devices ---------------------------------------------------------------------


def _snap(state: AppState, *, playing: bool, st: str | None, tracks: int = 2) -> dict:
    tr = [_track(i + 1) for i in range(tracks)]
    return {"tracks": [track_to_dict(t) for t in tr], "index": 0, "playing": playing,
            "state": st, "position_s": 5, "duration_s": 200, "volume": 40, "shuffle": False,
            "repeat": "off"}


def _device(state: AppState, pb_state: str) -> None:
    state.queue.load([_track(1), _track(2)], 0)
    pb = state.playback
    pb.active_host, pb.track, pb.state = "wiim-1", state.queue.current(), pb_state


def test_device_playing_something_else_is_not_shown_as_harmony_playing(state: AppState) -> None:
    """Harmony's queue is idle but the WiiM is playing Spotify Connect / AirPlay:
    showing "playing" would enable Pause/Stop that then drive the other app."""
    _device(state, "stopped")
    state._apply_snapshot("wiim-1", _snap(state, playing=False, st="playing"))
    assert state.playback.state == "stopped"
    assert state.can_stop() is False


def test_running_queue_on_a_playing_device_is_playing(state: AppState) -> None:
    _device(state, "stopped")
    state._apply_snapshot("wiim-1", _snap(state, playing=True, st="playing"))
    assert state.playback.state == "playing"
    assert state.can_stop() is True


def test_pause_is_not_undone_by_a_stale_snapshot(state: AppState, monkeypatch) -> None:
    _device(state, "playing")
    monkeypatch.setattr(state, "_remote_control", lambda *a, **k: None)
    state.playback_toggle_pause()
    assert state.playback.state == "paused"
    state._apply_snapshot("wiim-1", _snap(state, playing=True, st="playing"))  # pre-click read
    assert state.playback.state == "paused"
    state._apply_snapshot("wiim-1", _snap(state, playing=True, st="paused"))  # converged
    assert state.playback.state == "paused"
    state._apply_snapshot("wiim-1", _snap(state, playing=True, st="playing"))  # window over
    assert state.playback.state == "playing"  # a real resume elsewhere is trusted again


def test_settle_window_expires(state: AppState, monkeypatch) -> None:
    _device(state, "playing")
    monkeypatch.setattr(state, "_remote_control", lambda *a, **k: None)
    state.playback_toggle_pause()
    state._transport_until = 0.0
    state._apply_snapshot("wiim-1", _snap(state, playing=True, st="playing"))
    assert state.playback.state == "playing"


def test_stop_is_not_undone_by_a_stale_snapshot(state: AppState, monkeypatch) -> None:
    _device(state, "playing")
    monkeypatch.setattr(state, "_remote_op", lambda *a, **k: None)
    state.playback_stop()
    assert state.playback.state == "stopped"
    state._apply_snapshot("wiim-1", _snap(state, playing=False, st="playing"))
    assert state.playback.state == "stopped"


def test_play_from_stopped_on_device_shows_loading_and_holds(state: AppState, monkeypatch) -> None:
    _device(state, "stopped")
    ops: list[str] = []
    monkeypatch.setattr(state, "_remote_op", lambda op, *a, **k: ops.append(op))
    state._remote_has_queue = True
    state.playback_toggle_pause()
    assert state.playback.state == "loading"
    assert ops == ["jump"]
    # the idle status read that raced the load says stopped: still starting
    state._apply_snapshot("wiim-1", _snap(state, playing=False, st="stopped"))
    assert state.playback.state == "loading"
    state._apply_snapshot("wiim-1", _snap(state, playing=True, st="playing"))
    assert state.playback.state == "playing"


def test_error_snapshot_is_not_held_back(state: AppState) -> None:
    _device(state, "loading")
    state._note_transport("playing")
    snap = _snap(state, playing=False, st="stopped")
    snap["error"] = "Lost the connection"
    state._apply_snapshot("wiim-1", snap)
    assert state.playback.state == "stopped"


def test_poll_started_before_an_op_is_dropped(state: AppState, monkeypatch) -> None:
    _device(state, "playing")
    jobs: list = []
    monkeypatch.setattr(state_mod, "run_async",
                        lambda fn, done=None, failed=None, *a, **k: jobs.append(done))

    class Eng:
        def device_queue(self, host, via=None):
            return {}

    monkeypatch.setattr(state, "_engine", lambda: Eng())
    state._poll()
    state._poll()  # devices poll every other tick
    assert len(jobs) == 1
    monkeypatch.setattr(state, "_remote_control", lambda *a, **k: None)
    state.playback_toggle_pause()  # user pauses while the read is in flight
    jobs[0](_snap(state, playing=True, st="playing"))
    assert state.playback.state == "paused"
    assert state._poll_inflight is False
