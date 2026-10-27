"""When does a device queue decide a track ended?

A Chromecast says so itself (IDLE + FINISHED); the queue must advance on that
and never on a position/metadata guess that cuts the tail off the song."""

from __future__ import annotations

import time

import pytest

from harmony.playback import ChromecastDevice
from harmony.playback import chromecast as cc
from harmony.web import device_queue as dqmod
from harmony.web.cast import CastController
from harmony.web.device_queue import DeviceQueues


def _t(n: int, dur: int = 100) -> dict:
    return {"service": "qobuz", "id": str(n), "title": f"T{n}", "duration_s": dur}


@pytest.fixture
def dqs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dqmod, "SETTLE_S", 0.0)
    queues = DeviceQueues(lambda h, t: None, lambda h: {})
    monkeypatch.setattr(queues, "_ensure_poller", lambda host: None)
    return queues


def _cast_st(state: str, pos, dur, *, ended: bool = False, reason=None) -> dict:
    return {"state": state, "position_s": pos, "duration_s": dur, "ended": ended,
            "idle_reason": reason}


def test_cast_does_not_advance_near_the_end_it_waits_for_the_device(dqs) -> None:
    dqs.op("cast", "load", {"tracks": [_t(0, 180), _t(1)], "start": 0})
    dqs.after_status("cast", _cast_st("playing", 50, 180))
    # Within the old 2 s window of the end, and past the old poll threshold: the
    # device is still playing, so the song is not over.
    assert dqs.after_status("cast", _cast_st("playing", 178, 180)) is None
    assert dqs.after_status("cast", _cast_st("playing", 179, 180)) is None
    assert dqs.snapshot("cast")["playing"]
    assert dqs.after_status("cast", _cast_st("stopped", None, None, ended=True, reason="FINISHED")) == 1


def test_metadata_duration_shorter_than_the_real_file_never_ends_a_track(dqs) -> None:
    """YouTube metadata can undershoot; the device reports no duration and a
    moving position. Position past the metadata duration is not the end."""
    dqs.op("cast", "load", {"tracks": [_t(0, 100), _t(1)], "start": 0})
    for pos in (10, 60, 98, 99, 101, 130):
        assert dqs.after_status("cast", _cast_st("playing", pos, None)) is None
    assert dqs.after_status("cast", _cast_st("stopped", 0, None, ended=True, reason="FINISHED")) == 1


def test_natural_end_advances_even_if_the_track_never_looked_mid_playback(dqs) -> None:
    dqs.op("cast", "load", {"tracks": [_t(0, 3), _t(1)], "start": 0})  # shorter than a poll
    assert dqs.after_status("cast", _cast_st("stopped", None, None, ended=True, reason="FINISHED")) == 1


def test_last_track_finishing_ends_the_queue(dqs) -> None:
    dqs.op("cast", "load", {"tracks": [_t(0)], "start": 0})
    assert dqs.after_status("cast", _cast_st("stopped", None, None, ended=True, reason="FINISHED")) is None
    assert not dqs.snapshot("cast")["playing"]


def test_buffering_and_idle_without_a_finish_is_not_an_end(dqs) -> None:
    """A relay stall shows as BUFFERING (mapped to playing) or an IDLE that isn't
    FINISHED; neither advances, and a mid-track IDLE respects the stop."""
    dqs.op("cast", "load", {"tracks": [_t(0, 180), _t(1)], "start": 0})
    dqs.after_status("cast", _cast_st("playing", 60, 180))
    for _ in range(5):  # buffering at the same position
        assert dqs.after_status("cast", _cast_st("playing", 60, 180)) is None
    assert dqs.snapshot("cast")["playing"]
    assert dqs.after_status("cast", _cast_st("stopped", 60, 180, reason="CANCELLED")) is None
    assert not dqs.snapshot("cast")["playing"]


def test_receiver_error_skips_to_the_next_track(dqs) -> None:
    dqs.op("cast", "load", {"tracks": [_t(0, 180), _t(1)], "start": 0})
    dqs.after_status("cast", _cast_st("playing", 60, 180))
    assert dqs.after_status("cast", _cast_st("stopped", 60, 180, reason="ERROR")) == 1
    assert dqs.snapshot("cast")["playing"]  # the queue goes on


def test_receiver_error_on_the_last_track_halts_with_a_visible_error(dqs) -> None:
    dqs.op("cast", "load", {"tracks": [_t(0, 180)], "start": 0})
    dqs.after_status("cast", _cast_st("playing", 60, 180))
    assert dqs.after_status("cast", _cast_st("stopped", 60, 180, reason="ERROR")) is None
    snap = dqs.snapshot("cast")
    assert not snap["playing"] and "couldn't play" in snap["error"]


def test_three_receiver_errors_in_a_row_halt_the_queue(dqs) -> None:
    dqs.op("cast", "load", {"tracks": [_t(i) for i in range(6)], "start": 0})
    err = _cast_st("stopped", None, None, reason="ERROR")
    assert dqs.after_status("cast", err) == 1
    dqs._queues["cast"].q.jump(1)
    assert dqs.after_status("cast", err) == 2
    dqs._queues["cast"].q.jump(2)
    assert dqs.after_status("cast", err) is None  # the third
    snap = dqs.snapshot("cast")
    assert not snap["playing"] and "3 tracks in a row" in snap["error"]


def test_a_track_that_plays_to_its_end_resets_the_error_run(dqs) -> None:
    dqs.op("cast", "load", {"tracks": [_t(i) for i in range(8)], "start": 0})
    err = _cast_st("stopped", None, None, reason="ERROR")
    done = _cast_st("stopped", None, None, ended=True, reason="FINISHED")
    for i in range(2):
        assert dqs.after_status("cast", err) == i + 1
        dqs._queues["cast"].q.jump(i + 1)
    assert dqs.after_status("cast", done) == 3  # natural end: the run is over
    dqs._queues["cast"].q.jump(3)
    assert dqs.after_status("cast", err) == 4
    dqs._queues["cast"].q.jump(4)
    assert dqs.after_status("cast", err) == 5  # still not three in a row
    assert dqs.snapshot("cast")["playing"]


def test_error_wins_over_a_stale_finished_latch(dqs) -> None:
    """A failed LOAD with the previous track's FINISHED still latched is an
    error, not a finish: it counts toward the halt, not as a clean end."""
    dqs.op("cast", "load", {"tracks": [_t(i) for i in range(5)], "start": 0})
    st = _cast_st("stopped", None, None, ended=True, reason="ERROR")
    for i in range(2):
        assert dqs.after_status("cast", st) == i + 1
        dqs._queues["cast"].q.jump(i + 1)
    assert dqs.after_status("cast", st) is None
    assert "3 tracks in a row" in dqs.snapshot("cast")["error"]


def test_queue_loop_skips_errors_and_releases_the_device_on_the_final_halt(monkeypatch) -> None:
    played: list[str] = []
    inactive: list[str] = []
    monkeypatch.setattr(dqmod, "POLL_S", 0.01)
    monkeypatch.setattr(dqmod, "SETTLE_S", 0.0)
    err = _cast_st("stopped", None, None, reason="ERROR")
    queues = DeviceQueues(lambda h, t: played.append(t["id"]), lambda h: dict(err),
                          on_inactive=inactive.append)
    queues.op("cast", "load", {"tracks": [_t(i) for i in range(6)], "start": 0})
    for _ in range(200):
        if inactive:
            break
        time.sleep(0.02)
    assert played == ["0", "1", "2"]  # three tries, then it gave up
    assert inactive == ["cast"]
    snap = queues.snapshot("cast")
    assert not snap["playing"] and "3 tracks in a row" in snap["error"]


def test_settle_window_starts_after_the_track_is_on_the_device(monkeypatch) -> None:
    """A slow resolve + LOAD must not eat the settle window: the transitional
    IDLE a LOAD produces arrives after play returns."""
    monkeypatch.setattr(dqmod, "SETTLE_S", 5.0)

    def slow_play(host, track):
        time.sleep(0.3)

    queues = DeviceQueues(slow_play, lambda h: {})
    monkeypatch.setattr(queues, "_ensure_poller", lambda host: None)
    queues.op("cast", "load", {"tracks": [_t(0)], "start": 0})
    left = queues._queues["cast"].settle_until - time.monotonic()
    assert left > 4.9  # would be ~4.7 if the window began before the 0.3 s start


def test_wiim_without_a_natural_end_report_still_advances(dqs) -> None:
    """LinkPlay reports position + duration only: near the end of ITS duration, or
    the playing->stopped edge, still advance."""
    dqs.op("wiim", "load", {"tracks": [_t(0, 100), _t(1), _t(2)], "start": 0})
    dqs.after_status("wiim", {"state": "playing", "position_s": 40, "duration_s": 100})
    assert dqs.after_status("wiim", {"state": "playing", "position_s": 98, "duration_s": 100}) == 1
    dqs.op("wiim", "jump", {"index": 1})
    dqs.after_status("wiim", {"state": "playing", "position_s": 97, "duration_s": 100})
    assert dqs.after_status("wiim", {"state": "stopped", "position_s": 0, "duration_s": 100}) == 2


def test_wiim_ignores_the_metadata_duration_for_near_end(dqs) -> None:
    dqs.op("wiim", "load", {"tracks": [_t(0, 100), _t(1)], "start": 0})
    dqs.after_status("wiim", {"state": "playing", "position_s": 40, "duration_s": None})
    assert dqs.after_status("wiim", {"state": "playing", "position_s": 99, "duration_s": None}) is None


# -- Chromecast backend: the receiver's FINISHED is latched, not polled ---------


class _Status:
    def __init__(self, state: str, reason=None, t: float = 0, session=None) -> None:
        self.player_state, self.idle_reason = state, reason
        self.media_session_id = session
        self.current_time, self.duration, self.title = t, 200, "Song"


class _MC:
    def __init__(self) -> None:
        self.status = _Status("IDLE")
        self.listeners: list = []

    def register_status_listener(self, listener) -> None:
        self.listeners.append(listener)

    def push(self, status: _Status) -> None:  # what pychromecast does per message
        self.status = status
        for listener in self.listeners:
            listener.new_media_status(status)

    def play_media(self, *a, **k) -> None:
        pass

    def block_until_active(self, timeout=None) -> None:
        pass

    def update_status(self, callback_function=None) -> None:
        pass


class _Cast:
    def __init__(self) -> None:
        self.media_controller = _MC()
        self.status = type("S", (), {"volume_level": 0.5, "volume_muted": False,
                                     "session_id": "app-A", "transport_id": "tr-A"})()


@pytest.fixture
def cast(monkeypatch: pytest.MonkeyPatch) -> _Cast:
    c = _Cast()
    cc._CONNECTIONS.clear()
    cc._CONNECTIONS["10.0.0.9"] = c
    monkeypatch.setattr(cc, "_connect", lambda *a, **k: c)
    return c


def test_finished_is_remembered_across_a_later_status_without_a_reason(cast: _Cast) -> None:
    dev = ChromecastDevice("10.0.0.9")
    dev.play_url("http://relay/t", title="Song")
    mc = cast.media_controller
    mc.push(_Status("PLAYING", t=5))
    assert dev.status().raw["ended"] is False
    mc.push(_Status("IDLE", "FINISHED"))
    mc.push(_Status("IDLE", None))  # pychromecast clears idle_reason here
    st = dev.status()
    assert st.state == "stopped" and st.raw["ended"] is True


def test_a_new_load_forgets_the_previous_tracks_finish(cast: _Cast) -> None:
    dev = ChromecastDevice("10.0.0.9")
    dev.play_url("http://relay/1", title="One")
    cast.media_controller.push(_Status("IDLE", "FINISHED"))
    assert dev.status().raw["ended"] is True
    dev.play_url("http://relay/2", title="Two")  # controller still holds the stale FINISHED
    assert dev.status().raw["ended"] is False
    cast.media_controller.push(_Status("PLAYING", t=1))
    assert dev.status().raw["ended"] is False


def test_user_stop_is_not_a_finish(cast: _Cast) -> None:
    dev = ChromecastDevice("10.0.0.9")
    dev.play_url("http://relay/1", title="One")
    cast.media_controller.push(_Status("PLAYING", t=30))
    cast.media_controller.push(_Status("IDLE", "CANCELLED"))
    assert dev.status().raw == {"idle_reason": "CANCELLED", "ended": False}


def test_castcontroller_status_exposes_the_natural_end(cast: _Cast) -> None:
    ctl = CastController(lambda s, t: None)
    ChromecastDevice("10.0.0.9").play_url("http://relay/1", title="One")
    cast.media_controller.push(_Status("IDLE", "FINISHED"))
    st = ctl.status("10.0.0.9", kind="cast")
    assert st["ended"] is True and st["idle_reason"] == "FINISHED"


def test_queue_loop_advances_on_the_devices_natural_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whole loop with a fake Chromecast: a real poller thread, track 1 plays to
    its end, the receiver reports FINISHED, track 2 starts."""
    played: list[str] = []
    state = {"st": _cast_st("playing", 10, 180)}
    monkeypatch.setattr(dqmod, "POLL_S", 0.01)
    monkeypatch.setattr(dqmod, "SETTLE_S", 0.0)
    queues = DeviceQueues(lambda h, t: played.append(t["id"]), lambda h: dict(state["st"]))
    queues.op("cast", "load", {"tracks": [_t(0, 180), _t(1, 180)], "start": 0})
    time.sleep(0.1)
    assert played == ["0"]  # position near the metadata end does nothing
    state["st"] = _cast_st("playing", 179, 180)
    time.sleep(0.1)
    assert played == ["0"]
    state["st"] = _cast_st("stopped", None, None, ended=True, reason="FINISHED")
    for _ in range(100):
        if len(played) > 1:
            break
        time.sleep(0.02)
    assert played[:2] == ["0", "1"]
    queues.stop("cast")


def test_a_late_status_for_the_old_session_does_not_rearm_the_finish(cast: _Cast) -> None:
    """pychromecast re-fires its listeners with the cached IDLE/FINISHED (empty
    updates, a GET_STATUS reply) after play_url reset the latch; that old session's
    finish must not end the NEW track."""
    dev = ChromecastDevice("10.0.0.9")
    mc = cast.media_controller
    dev.play_url("http://relay/1", title="One")
    mc.push(_Status("PLAYING", t=5, session=11))
    mc.push(_Status("IDLE", "FINISHED", session=11))
    assert dev.status().raw["ended"] is True
    dev.play_url("http://relay/2", title="Two")  # LOAD sent; session 12 not reported yet
    assert dev.status().raw["ended"] is False
    mc.push(_Status("IDLE", "FINISHED", session=11))  # the stale reply lands late
    assert dev.status().raw["ended"] is False
    mc.push(_Status("PLAYING", t=1, session=12))
    mc.push(_Status("IDLE", "FINISHED", session=12))  # the new track's own end counts
    assert dev.status().raw["ended"] is True


def test_skip_message_survives_the_next_start_until_a_natural_end_or_user_action(dqs) -> None:
    """"<host> couldn't play <title>" is set by after_status() and must still be
    visible after the poll loop has started the next track (it used to be wiped
    by _start_track before any snapshot saw it)."""
    dqs.op("cast", "load", {"tracks": [_t(0), _t(1), _t(2)], "start": 0, "sync": True})
    nxt = dqs.after_status("cast", _cast_st("stopped", None, None, reason="ERROR"))
    assert nxt == 1
    assert dqs.snapshot("cast")["error"] == "cast couldn't play T0"
    dqs._start_track("cast", nxt, keep_error=True)  # what the poll loop does next
    snap = dqs.snapshot("cast")
    assert snap["error"] == "cast couldn't play T0"
    assert snap["playing"] and snap["current"]["id"] == "1"
    # a natural end of the next track clears it...
    assert dqs.after_status("cast", _cast_st("stopped", None, None, ended=True,
                                             reason="FINISHED")) == 2
    assert dqs.snapshot("cast")["error"] is None


def test_skip_message_is_cleared_by_a_user_action(dqs) -> None:
    dqs.op("cast", "load", {"tracks": [_t(0), _t(1), _t(2)], "start": 0, "sync": True})
    nxt = dqs.after_status("cast", _cast_st("stopped", None, None, reason="ERROR"))
    dqs._start_track("cast", nxt, keep_error=True)
    assert dqs.snapshot("cast")["error"]
    dqs.op("cast", "jump", {"index": 2, "sync": True})
    assert dqs.snapshot("cast")["error"] is None


def test_skip_message_survives_a_failed_start_that_skips_ahead(dqs, monkeypatch) -> None:
    plays: list[str] = []

    def play(_host, track):
        plays.append(track["id"])
        if track["id"] == "1":
            raise RuntimeError("boom")

    queues = DeviceQueues(play, lambda h: {})
    monkeypatch.setattr(queues, "_ensure_poller", lambda host: None)
    queues.op("cast", "load", {"tracks": [_t(0), _t(1), _t(2)], "start": 1, "sync": True})
    assert plays == ["1", "2"]  # track 1 failed, 2 started
    assert queues.snapshot("cast")["error"] == "boom"


def test_a_relaunched_receiver_app_with_colliding_session_ids_still_ends_the_track(
        cast: _Cast) -> None:
    """Media session ids restart from 1 when the receiver app is relaunched on the
    same connection. The stale-session ignore is tied to the receiver app instance,
    so the new app's FINISHED for an id that collides with the old one is honoured
    (it used to be swallowed, hanging the queue on a finished track)."""
    dev = ChromecastDevice("10.0.0.9")
    mc = cast.media_controller
    dev.play_url("http://relay/1", title="One")
    mc.push(_Status("PLAYING", t=5, session=3))
    mc.push(_Status("IDLE", "FINISHED", session=3))
    dev.play_url("http://relay/2", title="Two")  # ignores session 3 *of app A*
    mc.push(_Status("IDLE", "FINISHED", session=3))  # a stale replay from app A
    assert dev.status().raw["ended"] is False
    # the receiver app is relaunched: new app session, media ids restart (3 again)
    cast.status.session_id, cast.status.transport_id = "app-B", "tr-B"
    mc.push(_Status("PLAYING", t=1, session=3))
    mc.push(_Status("IDLE", "FINISHED", session=3))
    assert dev.status().raw["ended"] is True


def test_relaunch_before_the_replay_does_not_ignore_a_colliding_finish(cast: _Cast) -> None:
    dev = ChromecastDevice("10.0.0.9")
    mc = cast.media_controller
    dev.play_url("http://relay/1", title="One")
    mc.push(_Status("IDLE", "FINISHED", session=1))
    dev.play_url("http://relay/2", title="Two")
    cast.status.session_id = "app-B"  # relaunched before any status arrived
    mc.push(_Status("IDLE", "FINISHED", session=1))  # the new app's own session 1 ends
    assert dev.status().raw["ended"] is True
