"""The shared queue model (desktop + server device queues) and the server's
device auto-advance loop — no devices, no threads started."""

from __future__ import annotations

import time

import pytest

from harmony.playqueue import PlayQueue
from harmony.web import device_queue as dqmod
from harmony.web.device_queue import DeviceQueues


def _t(n: int) -> dict:
    return {"service": "qobuz", "id": str(n), "title": f"T{n}", "duration_s": 100}


def test_load_plays_the_whole_list_from_the_clicked_track() -> None:
    q = PlayQueue()
    tracks = [_t(i) for i in range(5)]
    assert q.load(tracks, 2) == 2
    assert q.current() is tracks[2]
    assert q.upcoming() == tracks[3:]


def test_shuffle_play_keeps_every_track_once() -> None:
    q = PlayQueue()
    tracks = [_t(i) for i in range(20)]
    q.load(tracks, None, shuffle=True)
    assert q.index == 0 and sorted(t["id"] for t in q.tracks) == sorted(t["id"] for t in tracks)


def test_shuffle_keeps_the_clicked_track_first() -> None:
    q = PlayQueue()
    tracks = [_t(i) for i in range(10)]
    q.load(tracks, 7, shuffle=True)
    assert q.current() is tracks[7]


def test_repeat_one_holds_on_natural_end_but_next_moves_on() -> None:
    q = PlayQueue()
    q.load([_t(0), _t(1)], 0)
    q.set_repeat("one")
    assert q.following() == 0
    assert q.advance(manual=True) == 1


def test_repeat_all_wraps_and_off_ends() -> None:
    q = PlayQueue()
    q.load([_t(0), _t(1)], 1)
    assert q.following() is None
    q.set_repeat("all")
    assert q.following() == 0


def test_previous_restarts_after_three_seconds() -> None:
    q = PlayQueue()
    q.load([_t(0), _t(1)], 1)
    assert q.previous(position_s=30) == 1
    assert q.previous(position_s=1) == 0


def test_enqueue_while_playing_appends_and_doesnt_interrupt() -> None:
    q = PlayQueue()
    q.load([_t(0)], 0)
    assert q.enqueue([_t(1), _t(2)], idle=False) is None
    assert q.index == 0 and [t["id"] for t in q.tracks] == ["0", "1", "2"]


def test_enqueue_when_idle_starts_the_added_tracks() -> None:
    q = PlayQueue()
    assert q.enqueue([_t(1)], idle=True) == 0


def test_play_next_inserts_after_current() -> None:
    q = PlayQueue()
    q.load([_t(0), _t(1)], 0)
    q.play_next([_t(9)], idle=False)
    assert [t["id"] for t in q.tracks] == ["0", "9", "1"]


def test_jump_to_any_row_including_play_next_items() -> None:
    q = PlayQueue()
    q.load([_t(0), _t(1)], 0)
    q.play_next([_t(9)], idle=False)
    assert q.jump(1) == 1 and q.current()["id"] == "9"


def test_move_keeps_the_current_track_playing() -> None:
    q = PlayQueue()
    q.load([_t(0), _t(1), _t(2)], 1)
    q.move(2, 0)
    assert q.current()["id"] == "1" and q.index == 2


def test_remove_current_hands_off_to_the_next_track() -> None:
    q = PlayQueue()
    q.load([_t(0), _t(1), _t(2)], 1)
    removed_current, start = q.remove(1)
    assert removed_current and start == 1 and q.current()["id"] == "2"


def test_clear_keeps_only_whats_playing() -> None:
    q = PlayQueue()
    q.load([_t(0), _t(1), _t(2)], 1)
    q.clear()
    assert [t["id"] for t in q.tracks] == ["1"] and q.index == 0


def test_shuffle_off_restores_load_order_on_the_same_track() -> None:
    q = PlayQueue()
    tracks = [_t(i) for i in range(8)]
    q.load(tracks, 3)
    q.set_shuffle(True)
    q.set_shuffle(False)
    assert q.tracks == tracks and q.current() is tracks[3]


# -- server device loop -------------------------------------------------------


@pytest.fixture
def dqs(monkeypatch: pytest.MonkeyPatch):
    played: list[tuple[str, str]] = []
    monkeypatch.setattr(dqmod, "SETTLE_S", 0.0)
    queues = DeviceQueues(lambda h, t: played.append((h, t["id"])), lambda h: {})
    monkeypatch.setattr(queues, "_ensure_poller", lambda host: None)
    return queues, played


def test_load_starts_the_clicked_track_on_the_device(dqs) -> None:
    queues, played = dqs
    snap = queues.op("wiim", "load", {"tracks": [_t(0), _t(1), _t(2)], "start": 1})
    assert played == [("wiim", "1")] and snap["index"] == 1 and snap["playing"]


def test_device_advances_when_progress_reaches_the_end(dqs) -> None:
    queues, played = dqs
    queues.op("wiim", "load", {"tracks": [_t(0), _t(1)], "start": 0})
    assert queues.after_status("wiim", {"state": "playing", "position_s": 50, "duration_s": 100}) is None
    nxt = queues.after_status("wiim", {"state": "playing", "position_s": 99, "duration_s": 100})
    assert nxt == 1


def test_frozen_position_device_advances_by_play_clock(dqs, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Chromecast on a relayed stream reports position 0 forever; the play
    clock stands in so the queue still advances when the device goes idle."""
    queues, _played = dqs
    queues.op("cast", "load", {"tracks": [_t(0), _t(1)], "start": 0})
    now = [time.monotonic()]
    monkeypatch.setattr(dqmod.time, "monotonic", lambda: now[0])
    queues.after_status("cast", {"state": "playing", "position_s": 0, "duration_s": 100})
    for _ in range(48):
        now[0] += 2
        assert queues.after_status("cast", {"state": "playing", "position_s": 0, "duration_s": 100}) is None
    assert queues.snapshot("cast")["position_s"] == 96                # clock-driven progress
    now[0] += 2
    assert queues.after_status("cast", {"state": "playing", "position_s": 0, "duration_s": 100}) == 1


def test_stop_from_the_device_mid_track_halts_instead_of_skipping(dqs) -> None:
    queues, _played = dqs
    queues.op("wiim", "load", {"tracks": [_t(0), _t(1)], "start": 0})
    queues.after_status("wiim", {"state": "playing", "position_s": 30, "duration_s": 100})
    assert queues.after_status("wiim", {"state": "stopped", "position_s": 0, "duration_s": 100}) is None
    assert not queues.snapshot("wiim")["playing"]


def test_a_failing_track_is_skipped_but_all_failing_halts(monkeypatch: pytest.MonkeyPatch) -> None:
    def play(_h, t):
        raise RuntimeError("nope")

    queues = DeviceQueues(play, lambda h: {})
    monkeypatch.setattr(queues, "_ensure_poller", lambda host: None)
    snap = queues.op("wiim", "load", {"tracks": [_t(0), _t(1)], "start": 0})
    assert not snap["playing"] and snap["error"]


def test_enqueue_during_playback_doesnt_restart(dqs) -> None:
    queues, played = dqs
    queues.op("wiim", "load", {"tracks": [_t(0)], "start": 0})
    queues.op("wiim", "enqueue", {"tracks": [_t(5)]})
    assert played == [("wiim", "0")]
    assert [t["id"] for t in queues.snapshot("wiim")["tracks"]] == ["0", "5"]
