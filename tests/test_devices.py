"""Offline tests for the device-management state layer (AppState + Settings).

Nothing here touches the network, a real WiiM device, the system keyring, or
the user's real config/data dirs: ``_make_state`` builds an ``AppState``
without running its normal ``__init__`` (which opens the db, probes the
keyring, and constructs providers) and points ``Settings`` at a tmp file
instead. ``device_for`` is exercised too, but only as far as *constructing*
a ``WiiMDevice`` — construction does no I/O, only the methods called on the
result would, and this file never calls any of those.
"""

from __future__ import annotations

import pytest

# These exercise the UI state layer, which imports PyGObject. The multi-version
# offline CI job has no GTK, so skip cleanly there; the GTK ui-smoke job runs them.
pytest.importorskip("gi")

from gi.repository import GObject  # noqa: E402

from harmony import config as config_module  # noqa: E402
from harmony.models import Service, StreamSource, Track  # noqa: E402
from harmony.playback import DeviceInfo, WiiMDevice  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> AppState:
    """A bare ``AppState`` wired to an isolated ``Settings`` file.

    Bypasses ``AppState.__init__`` entirely (no db, no CredentialStore, no
    provider construction, no worker-thread reload) since the device
    methods under test only ever touch ``self.settings`` and the GObject
    signal machinery. Still a real ``AppState`` instance -- these are the
    actual bound methods, not a reimplementation of their logic.
    """
    monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")
    obj = AppState.__new__(AppState)
    GObject.Object.__init__(obj)
    obj.settings = config_module.Settings.load()
    obj._device_session = None
    obj._local_player = None
    # Playback model + queue engine state that __init__ would have set up.
    from harmony.ui.state import PlaybackState

    obj.playback = PlaybackState()
    obj._now_playing = {}
    obj._upnp_cache = {}
    obj._queues = {}
    obj._queue_prev_state = {}
    obj._queue_armed = {}
    obj._queue_poll_ids = {}
    obj._collection_full = {}
    obj._collection_key = {}
    obj._history = {}
    return obj


def _signal_recorder(state: AppState, name: str) -> list[None]:
    calls: list[None] = []
    state.connect(name, lambda *_a: calls.append(None))
    return calls


# -- add_device ---------------------------------------------------------------


def test_add_device_persists_and_emits(state: AppState) -> None:
    changed = _signal_recorder(state, "devices-changed")

    state.add_device("192.168.1.50", "Living Room")

    assert state.settings.known_devices == [
        {"host": "192.168.1.50", "name": "Living Room", "kind": "wiim"}
    ]
    assert len(changed) == 1


def test_add_device_defaults_name_to_host(state: AppState) -> None:
    state.add_device("wiim.local")

    assert state.settings.known_devices == [
        {"host": "wiim.local", "name": "wiim.local", "kind": "wiim"}
    ]


def test_add_device_dedupes_by_host(state: AppState) -> None:
    state.add_device("192.168.1.50", "Living Room")
    changed = _signal_recorder(state, "devices-changed")

    state.add_device("192.168.1.50", "Some Other Name")

    assert len(state.settings.known_devices) == 1
    assert state.settings.known_devices[0]["name"] == "Living Room"
    assert changed == []  # second add was a no-op, no spurious signal


def test_add_device_ignores_blank_host(state: AppState) -> None:
    state.add_device("   ")

    assert state.settings.known_devices == []


def test_add_device_two_distinct_hosts(state: AppState) -> None:
    state.add_device("192.168.1.50", "Living Room")
    state.add_device("192.168.1.51", "Kitchen")

    hosts = {d["host"] for d in state.settings.known_devices}
    assert hosts == {"192.168.1.50", "192.168.1.51"}


# -- remove_device --------------------------------------------------------------


def test_remove_device_removes_matching_host(state: AppState) -> None:
    state.add_device("192.168.1.50", "Living Room")
    state.add_device("192.168.1.51", "Kitchen")
    changed = _signal_recorder(state, "devices-changed")

    state.remove_device("192.168.1.50")

    assert [d["host"] for d in state.settings.known_devices] == ["192.168.1.51"]
    assert len(changed) == 1


def test_remove_device_missing_host_is_noop(state: AppState) -> None:
    state.add_device("192.168.1.50", "Living Room")
    changed = _signal_recorder(state, "devices-changed")

    state.remove_device("10.0.0.99")

    assert len(state.settings.known_devices) == 1
    assert changed == []


# -- known_devices ----------------------------------------------------------------


def test_known_devices_maps_settings_dicts_to_device_info(state: AppState) -> None:
    state.add_device("192.168.1.50", "Living Room")
    state.add_device("192.168.1.51", "Kitchen")

    devices = state.known_devices()

    assert all(isinstance(d, DeviceInfo) for d in devices)
    by_host = {d.host: d for d in devices}
    assert by_host["192.168.1.50"].name == "Living Room"
    assert by_host["192.168.1.50"].kind == "wiim"
    assert by_host["192.168.1.51"].name == "Kitchen"


def test_known_devices_empty_by_default(state: AppState) -> None:
    assert state.known_devices() == []


def test_known_devices_skips_entries_without_a_host(state: AppState) -> None:
    state.settings.known_devices.append({"name": "Orphan", "kind": "wiim"})

    assert state.known_devices() == []


# -- set_device_name ------------------------------------------------------------


def test_set_device_name_updates_and_emits(state: AppState) -> None:
    state.add_device("192.168.1.50")  # name defaults to host
    changed = _signal_recorder(state, "devices-changed")

    state.set_device_name("192.168.1.50", "Living Room WiiM Pro")

    assert state.settings.known_devices[0]["name"] == "Living Room WiiM Pro"
    assert len(changed) == 1


def test_set_device_name_noop_when_unchanged(state: AppState) -> None:
    state.add_device("192.168.1.50", "Living Room")
    changed = _signal_recorder(state, "devices-changed")

    state.set_device_name("192.168.1.50", "Living Room")

    assert changed == []


# -- device_for -----------------------------------------------------------------


def test_device_for_constructs_wiim_device_without_io(state: AppState) -> None:
    device = state.device_for("192.0.2.10")

    assert isinstance(device, WiiMDevice)
    assert device.host == "192.0.2.10"


def test_device_for_reuses_shared_session(state: AppState) -> None:
    first = state.device_for("192.0.2.10")
    second = state.device_for("192.0.2.11")

    assert first._session is second._session  # noqa: SLF001 - verifying pooling, not public API


# -- round-trip through Settings.save()/load() -----------------------------------


def test_known_devices_round_trips_through_save_and_load(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")

    original = config_module.Settings(
        known_devices=[
            {"host": "192.168.1.50", "name": "Living Room", "kind": "wiim"},
            {"host": "192.168.1.51", "name": "Kitchen", "kind": "wiim"},
        ]
    )
    original.save()

    loaded = config_module.Settings.load()

    assert loaded.known_devices == original.known_devices


def test_known_devices_defaults_to_empty_list(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")

    assert config_module.Settings.load().known_devices == []


def test_add_device_survives_a_reload(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """AppState.add_device saves via Settings.save(); a fresh Settings.load() sees it."""
    monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")
    obj = AppState.__new__(AppState)
    GObject.Object.__init__(obj)
    obj.settings = config_module.Settings.load()
    obj._device_session = None

    obj.add_device("192.168.1.50", "Living Room")

    reloaded = config_module.Settings.load()
    assert reloaded.known_devices == [{"host": "192.168.1.50", "name": "Living Room", "kind": "wiim"}]


# -- play_track_on_device: UPnP-first with an httpapi fallback ----------------


class _FakeProvider:
    service = Service.YTMUSIC

    def resolve_stream(self, track_id: str, *, max_quality: bool = False) -> StreamSource:
        return StreamSource(url="http://cdn/stream", mime_type="audio/mp4", container="m4a")


class _FakeRelay:
    def __init__(self) -> None:
        self.registered: list[dict] = []

    def register(self, resolver, *, title=None, artist=None, allow_icy=True) -> str:
        self.registered.append({"title": title, "artist": artist, "allow_icy": allow_icy})
        return "tok"

    def url_for(self, token: str, host: str) -> str:
        return f"http://relay/{token}"


def _ready_to_play(state: AppState) -> AppState:
    state._relay = _FakeRelay()
    state._now_playing = {}
    state._upnp_cache = {}
    state.providers = {Service.YTMUSIC: _FakeProvider()}
    return state


def _a_track() -> Track:
    return Track(id="vid", title="Song", service=Service.YTMUSIC, artists=["Artist"],
                 album="Album", duration_s=222, artwork_url="http://art")


def test_play_uses_upnp_when_available(state: AppState, monkeypatch) -> None:
    _ready_to_play(state)
    played: dict = {}

    class _Renderer:
        def play_media(self, url, **kw):
            played.update(url=url, **kw)

    def _no_httpapi(host):
        raise AssertionError("httpapi must not be used when UPnP works")

    monkeypatch.setattr(state, "_upnp_renderer_for", lambda host: _Renderer())
    monkeypatch.setattr(state, "device_for", _no_httpapi)

    state.play_track_on_device(_a_track(), "192.168.1.9")

    assert played["url"] == "http://relay/tok"
    assert played["title"] == "Song" and played["artist"] == "Artist"
    assert played["duration_s"] == 222 and played["mime"] == "audio/mp4"
    assert state._relay.registered[-1]["allow_icy"] is False  # passthrough for UPnP
    assert state._now_playing["192.168.1.9"] == ("Song", "Artist")


def test_play_falls_back_to_httpapi_without_upnp(state: AppState, monkeypatch) -> None:
    _ready_to_play(state)
    played: dict = {}

    class _Device:
        def play_url(self, url):
            played["url"] = url

    monkeypatch.setattr(state, "_upnp_renderer_for", lambda host: None)
    monkeypatch.setattr(state, "device_for", lambda host: _Device())

    state.play_track_on_device(_a_track(), "192.168.1.9")

    assert played["url"] == "http://relay/tok"
    assert state._relay.registered[-1]["allow_icy"] is True  # ICY best-effort for httpapi


def test_play_falls_back_when_upnp_raises(state: AppState, monkeypatch) -> None:
    _ready_to_play(state)
    played: dict = {}

    class _BadRenderer:
        def play_media(self, url, **kw):
            raise RuntimeError("no route to device")

    class _Device:
        def play_url(self, url):
            played["url"] = url

    monkeypatch.setattr(state, "_upnp_renderer_for", lambda host: _BadRenderer())
    monkeypatch.setattr(state, "device_for", lambda host: _Device())

    state.play_track_on_device(_a_track(), "192.168.1.9")

    assert played["url"] == "http://relay/tok"  # UPnP failed -> httpapi still played it


# -- play_tracks_on_device: album/playlist queue advance ----------------------


def _track_n(n: int) -> Track:
    return Track(id=f"t{n}", title=f"Song {n}", service=Service.YTMUSIC, artists=["A"])


def _seed_queue(state: AppState, tracks: list) -> None:
    state._queues = {"h": tracks}
    state._queue_prev_state = {"h": ""}
    state._queue_poll_ids = {}
    state._queue_armed = {"h": False}


def test_queue_advances_when_position_reaches_duration(state: AppState) -> None:
    _seed_queue(state, [_track_n(1), _track_n(2), _track_n(3)])
    assert state._next_after_status("h", "playing", 10, 200) is None  # mid-track: arm, no advance
    nxt = state._next_after_status("h", "playing", 199, 200)          # near end -> advance
    assert nxt is not None and nxt.id == "t2"
    assert [t.id for t in state._queues["h"]] == ["t2", "t3"]


def test_queue_progress_advances_exactly_once(state: AppState) -> None:
    _seed_queue(state, [_track_n(1), _track_n(2)])
    state._next_after_status("h", "playing", 10, 200)                 # arm
    assert state._next_after_status("h", "playing", 199, 200).id == "t2"  # advance
    # A second near-end reading before the next track starts must not advance again.
    assert state._next_after_status("h", "playing", 200, 200) is None
    assert [t.id for t in state._queues["h"]] == ["t2"]


def test_queue_rearms_for_the_next_track(state: AppState) -> None:
    _seed_queue(state, [_track_n(1), _track_n(2)])
    state._next_after_status("h", "playing", 10, 200)                 # arm t1
    state._next_after_status("h", "playing", 199, 200)               # advance to t2, disarm
    assert state._next_after_status("h", "playing", 5, 200) is None   # re-arm on t2
    assert state._next_after_status("h", "playing", 199, 200) is None  # t2 was last -> clear
    assert "h" not in state._queues


def test_queue_advance_records_history(state: AppState) -> None:
    _seed_queue(state, [_track_n(1), _track_n(2)])
    state._next_after_status("h", "playing", 10, 200)   # arm
    state._next_after_status("h", "playing", 199, 200)  # advance t1 -> t2
    assert [t.id for t in state._history["h"]] == ["t1"]  # finished track remembered for "previous"


def test_repeat_one_replays_current_track(state: AppState) -> None:
    _seed_queue(state, [_track_n(1), _track_n(2)])
    state.playback.repeat = "one"
    state._next_after_status("h", "playing", 10, 200)          # arm
    nxt = state._next_after_status("h", "playing", 199, 200)   # near end
    assert nxt is not None and nxt.id == "t1"                  # same track again
    assert [t.id for t in state._queues["h"]] == ["t1", "t2"]  # queue untouched


def test_repeat_all_refills_after_last_track(state: AppState) -> None:
    _seed_queue(state, [_track_n(1)])
    state._collection_full = {"h": [_track_n(1), _track_n(2)]}
    state.playback.repeat = "all"
    state._next_after_status("h", "playing", 10, 200)          # arm
    nxt = state._next_after_status("h", "playing", 199, 200)   # last track ends -> wrap
    assert nxt is not None and nxt.id == "t1"
    assert [t.id for t in state._queues["h"]] == ["t1", "t2"]  # refilled from the collection


def test_queue_does_not_advance_while_mid_track(state: AppState) -> None:
    _seed_queue(state, [_track_n(1), _track_n(2)])
    assert state._next_after_status("h", "playing", 30, 200) is None
    assert len(state._queues["h"]) == 2


def test_queue_falls_back_to_stopped_without_duration(state: AppState) -> None:
    _seed_queue(state, [_track_n(1), _track_n(2)])
    state._queue_prev_state = {"h": "playing"}
    nxt = state._next_after_status("h", "stopped", None, None)  # no duration -> state edge
    assert nxt is not None and nxt.id == "t2"


def test_queue_clears_after_last_track(state: AppState) -> None:
    _seed_queue(state, [_track_n(1)])
    state._queue_prev_state = {"h": "playing"}
    assert state._next_after_status("h", "stopped", None, None) is None
    assert "h" not in state._queues  # emptied and cleared


def test_play_tracks_sets_queue_and_plays_head(state: AppState, monkeypatch) -> None:
    state._queues = {}
    state._queue_prev_state = {}
    state._queue_poll_ids = {}
    played: list = []
    monkeypatch.setattr(state, "_play_one", lambda t, h: played.append((t.id, h)))
    monkeypatch.setattr("harmony.ui.state.on_main", lambda fn, *a: None)  # don't start the real poller

    state.play_tracks_on_device([_track_n(1), _track_n(2), _track_n(3)], "192.168.1.9")

    assert played == [("t1", "192.168.1.9")]  # only the head plays synchronously
    assert [t.id for t in state._queues["192.168.1.9"]] == ["t1", "t2", "t3"]


def test_play_empty_track_list_is_a_noop(state: AppState, monkeypatch) -> None:
    state._queues = {}
    monkeypatch.setattr(state, "_play_one", lambda t, h: (_ for _ in ()).throw(AssertionError("must not play")))
    state.play_tracks_on_device([], "192.168.1.9")
    assert state._queues == {}
