"""Offline view tests for the Now Playing bar (GTK required; skipped where absent)."""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from gi.repository import GObject  # noqa: E402

from harmony import config as config_module  # noqa: E402
from harmony.models import Service, Track  # noqa: E402
from harmony.ui.now_playing_bar import NowPlayingBar  # noqa: E402
from harmony.ui.state import AppState, PlaybackState  # noqa: E402


@pytest.fixture
def state(monkeypatch, tmp_path) -> AppState:
    monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")
    obj = AppState.__new__(AppState)
    GObject.Object.__init__(obj)
    obj.settings = config_module.Settings.load()
    obj._device_session = None
    obj.playback = PlaybackState()
    for attr in ("_now_playing", "_upnp_cache", "_queues", "_queue_prev_state",
                 "_queue_armed", "_queue_poll_ids", "_collection_full",
                 "_collection_key", "_history"):
        setattr(obj, attr, {})
    return obj


def _play(state: AppState, **kw) -> None:
    pb = state.playback
    pb.track = Track(id="t1", title="Song", service=Service.YTMUSIC, artists=["Artist"],
                     duration_s=200)
    pb.state = "playing"
    pb.position_s = 42
    pb.duration_s = 200
    for key, value in kw.items():
        setattr(pb, key, value)


def test_bar_hidden_when_idle(state: AppState) -> None:
    bar = NowPlayingBar(state)
    assert bar.get_visible() is False


def test_bar_shows_track_and_pause_icon(state: AppState) -> None:
    bar = NowPlayingBar(state)
    _play(state)
    bar._render()
    assert bar.get_visible() is True
    assert bar._title.get_label() == "Song"
    assert bar._play.get_icon_name() == "media-playback-pause-symbolic"
    assert bar._pos.get_label() == "0:42"
    assert bar._dur.get_label() == "3:20"


def test_volume_hidden_until_supported(state: AppState) -> None:
    bar = NowPlayingBar(state)
    _play(state)
    bar._render()
    assert bar._vol_box.get_visible() is False
    _play(state, volume_supported=True, volume=60)
    bar._render()
    assert bar._vol_box.get_visible() is True


def test_repeat_button_cycles_state(state: AppState) -> None:
    bar = NowPlayingBar(state)
    _play(state)
    bar._render()
    assert state.playback.repeat == "off"
    bar._on_repeat_clicked(bar._repeat)
    assert state.playback.repeat == "all"
    bar._on_repeat_clicked(bar._repeat)
    assert state.playback.repeat == "one"
    bar._on_repeat_clicked(bar._repeat)
    assert state.playback.repeat == "off"


def test_prev_next_sensitivity_follows_flags(state: AppState) -> None:
    bar = NowPlayingBar(state)
    _play(state, has_prev=False, has_next=True)
    bar._render()
    assert bar._prev.get_sensitive() is False
    assert bar._next.get_sensitive() is True
