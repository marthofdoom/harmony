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
