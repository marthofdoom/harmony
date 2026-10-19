"""Chromecast backend + CastController routing — no real device, no pychromecast.

Verifies the wiring: discovery degrades cleanly without the optional dependency,
CastController routes ``kind="cast"`` to the Chromecast backend, and the backend
maps transport calls onto a (faked) CASTV2 connection.
"""

from __future__ import annotations

import pytest

from harmony.errors import NotSupportedError
from harmony.playback import ChromecastDevice, discover_cast
from harmony.playback import chromecast as cc
from harmony.web.cast import CastController


def test_discover_cast_without_dependency_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "_pychromecast", lambda: None)
    assert discover_cast(timeout=0.1) == []
    assert cc.available() is False


class _FakeMediaController:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.status = type("S", (), {"player_state": "PLAYING", "current_time": 12,
                                     "duration": 200, "title": "Song"})()

    def play_media(self, url, content_type=None, title=None, thumb=None, metadata=None):
        self.calls.append(f"play:{url}:{content_type}")
        self.last_metadata = metadata

    def block_until_active(self, timeout=None):
        pass

    def pause(self):
        self.calls.append("pause")

    def play(self):
        self.calls.append("play")

    def stop(self):
        self.calls.append("stop")


class _FakeCast:
    def __init__(self) -> None:
        self.media_controller = _FakeMediaController()
        self.status = type("S", (), {"volume_level": 0.5, "volume_muted": False})()
        self.volume: float | None = None

    def wait(self, timeout=None):
        pass

    def set_volume(self, level):
        self.volume = level

    def set_volume_muted(self, muted):
        pass

    def disconnect(self, blocking=False):
        pass


@pytest.fixture
def fake_cast(monkeypatch: pytest.MonkeyPatch) -> _FakeCast:
    cast = _FakeCast()
    cc._CONNECTIONS.clear()
    cc._CONNECTIONS["192.168.1.50"] = cast  # stop() only ever uses a live connection
    monkeypatch.setattr(cc, "_connect", lambda *a, **k: cast)
    return cast


def test_chromecast_device_transport(fake_cast: _FakeCast) -> None:
    dev = ChromecastDevice("192.168.1.50")
    dev.play_url("http://relay/track.mp3", mime="audio/mpeg", title="Song")
    dev.pause()
    dev.resume()
    dev.set_volume(80)
    st = dev.status()
    assert "play:http://relay/track.mp3:audio/mpeg" in fake_cast.media_controller.calls
    assert "pause" in fake_cast.media_controller.calls
    assert "play" in fake_cast.media_controller.calls
    assert fake_cast.volume == pytest.approx(0.8)
    assert st.state == "playing" and st.volume == 50


def test_chromecast_sends_full_track_metadata(fake_cast: _FakeCast) -> None:
    dev = ChromecastDevice("192.168.1.50")
    dev.play_url("http://relay/t.mp3", title="Five Years", artist="Bowie",
                 album="Ziggy Stardust", art_url="http://art/cover.jpg", duration_s=289)
    md = fake_cast.media_controller.last_metadata
    assert md["metadataType"] == 3
    assert md["title"] == "Five Years"
    assert md["artist"] == "Bowie"
    assert md["albumName"] == "Ziggy Stardust"
    assert md["images"] == [{"url": "http://art/cover.jpg"}]


def test_chromecast_has_no_queue_nav(fake_cast: _FakeCast) -> None:
    dev = ChromecastDevice("192.168.1.50")
    with pytest.raises(NotSupportedError):
        dev.next()


def test_castcontroller_routes_cast_kind(fake_cast: _FakeCast) -> None:
    ctrl = CastController(resolve_source=lambda s, t: None)
    device = ctrl._device("192.168.1.50", kind="cast", device_info={"name": "Living Room TV"})
    assert isinstance(device, ChromecastDevice)
    ctrl.control("192.168.1.50", "pause", kind="cast")
    assert "pause" in fake_cast.media_controller.calls


def test_castcontroller_defaults_to_wiim() -> None:
    ctrl = CastController(resolve_source=lambda s, t: None)
    device = ctrl._device("192.168.1.9")  # no kind -> WiiM
    assert device.__class__.__name__ == "WiiMDevice"


class _SessionlessMediaController(_FakeMediaController):
    """Like pychromecast on a connection that didn't start the cast: no session
    known until update_status() is asked, and stop() is then a silent no-op."""

    def __init__(self) -> None:
        super().__init__()
        self.status = None

    def update_status(self, callback_function=None):
        self.calls.append("update_status")
        self.status = type("S", (), {"media_session_id": 7, "player_state": "PLAYING"})()
        if callback_function:
            callback_function(True, None)

    def stop(self):
        if self.status is None:  # what pychromecast does: warn and drop the command
            return
        self.calls.append("stop")


def test_stop_works_from_a_connection_that_didnt_start_the_cast(fake_cast: _FakeCast) -> None:
    """The bug: Stop did nothing (no media session known) and the cast kept playing."""
    fake_cast.media_controller = _SessionlessMediaController()
    fake_cast.app_id = cc._MEDIA_RECEIVER_APP_ID
    quits: list[bool] = []
    fake_cast.quit_app = lambda timeout=10: quits.append(True)
    ChromecastDevice("192.168.1.50").stop()
    calls = fake_cast.media_controller.calls
    assert calls.index("update_status") < calls.index("stop")
    assert quits == [True]                     # the receiver app is closed: casting ends


def test_stop_never_quits_another_app_on_the_tv(fake_cast: _FakeCast) -> None:
    fake_cast.app_id = "SOME_OTHER_APP"
    quits: list[bool] = []
    fake_cast.quit_app = lambda timeout=10: quits.append(True)
    ChromecastDevice("192.168.1.50").stop()
    assert quits == []


def test_pause_refreshes_a_missing_session_first(fake_cast: _FakeCast) -> None:
    fake_cast.media_controller = _SessionlessMediaController()
    ChromecastDevice("192.168.1.50").pause()
    assert fake_cast.media_controller.calls[:2] == ["update_status", "pause"]


def test_after_stop_idle_polls_never_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop must disconnect and STAY disconnected: background status polls
    (every client polls the selected output) used to reopen the connection."""
    connects: list[str] = []
    cast = _FakeCast()
    cast.app_id = cc._MEDIA_RECEIVER_APP_ID
    cast.quit_app = lambda timeout=10: None

    def connect(host, *a, **k):
        connects.append(host)
        cc._CONNECTIONS[host] = cast
        return cast

    cc._CONNECTIONS.clear()
    monkeypatch.setattr(cc, "_connect", connect)
    dev = ChromecastDevice("192.168.1.50")
    dev.stop()
    assert "192.168.1.50" not in cc._CONNECTIONS          # disconnected
    n = len(connects)
    for _ in range(5):
        st = dev.status(connect=False)
        assert st.state == "stopped" and st.raw == {"connected": False}
    assert len(connects) == n                              # ...and stays disconnected
    dev.status()                                           # an explicit refresh may connect
    assert len(connects) == n + 1


def test_engine_idle_queue_poll_is_passive(monkeypatch: pytest.MonkeyPatch) -> None:
    from harmony.web.api import Engine

    e = Engine()
    seen: list[bool] = []
    monkeypatch.setattr(e, "device_status", lambda host, via=None, connect=True: seen.append(connect) or {})
    e.device_queue("192.168.1.50")
    assert seen == [False]
