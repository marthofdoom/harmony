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
    obj._init_playback()
    return obj


def _signal_recorder(state: AppState, name: str) -> list[None]:
    calls: list[None] = []
    state.connect(name, lambda *_a: calls.append(None))
    return calls


# -- auto-discovered devices flow into the list + pickers ---------------------


def test_discovered_devices_appear_without_saving(state: AppState) -> None:
    cast = DeviceInfo(id="uuid", name="Living Room", host="10.0.0.5", kind="cast")
    calls = _signal_recorder(state, "devices-changed")
    state.set_discovered_devices([cast])
    # Shows up in the merged list and the playback picker, but isn't persisted.
    assert [d.host for d in state.all_devices()] == ["10.0.0.5"]
    assert state.known_devices() == []
    assert state.is_saved_device("10.0.0.5") is False
    assert "10.0.0.5" in {d.host for d in state.playback_targets()}
    assert len(calls) == 1


def test_device_for_routes_a_discovered_chromecast(state: AppState) -> None:
    state.set_discovered_devices([DeviceInfo(id="u", name="TV", host="10.0.0.9", kind="cast")])
    assert state.device_for("10.0.0.9").__class__.__name__ == "ChromecastDevice"


def test_saved_device_wins_over_discovered_duplicate(state: AppState) -> None:
    state.add_device("10.0.0.5", "Saved", kind="wiim")
    state.set_discovered_devices([DeviceInfo(id="u", name="Dup", host="10.0.0.5", kind="cast")])
    hosts = [d.host for d in state.all_devices()]
    assert hosts == ["10.0.0.5"]  # deduped
    assert state.is_saved_device("10.0.0.5") is True


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


# -- device play: UPnP-first with an httpapi fallback (CastController) --------


class _FakeRelay:
    def __init__(self) -> None:
        self.registered: list[dict] = []

    def register(self, resolver, *, title=None, artist=None, allow_icy=True) -> str:
        self.registered.append({"title": title, "artist": artist, "allow_icy": allow_icy})
        return "tok"

    def url_for(self, token: str, host: str) -> str:
        return f"http://relay/{token}"


def _caster(monkeypatch, renderer, device=None):
    from harmony.web.cast import CastController

    ctrl = CastController(lambda s, t: StreamSource(url="http://cdn/stream", mime_type="audio/mp4",
                                                     container="m4a"))
    ctrl._relay = _FakeRelay()
    monkeypatch.setattr(ctrl, "_upnp_renderer", lambda host: renderer)
    monkeypatch.setattr(ctrl, "_device", lambda host, kind="wiim", info=None: device)
    return ctrl


_META = {"title": "Song", "artist": "Artist", "album": "Album", "duration_s": 222, "art_url": "http://art"}


def test_cast_uses_upnp_when_available(monkeypatch) -> None:
    played: dict = {}

    class _Renderer:
        def play_media(self, url, **kw):
            played.update(url=url, **kw)

    ctrl = _caster(monkeypatch, _Renderer())
    ctrl.cast("192.168.1.9", "ytmusic", "vid", _META)
    assert played["url"] == "http://relay/tok"
    assert played["title"] == "Song" and played["artist"] == "Artist"
    assert played["duration_s"] == 222 and played["mime"] == "audio/mp4"
    assert ctrl._relay.registered[-1]["allow_icy"] is False  # passthrough for UPnP


def test_cast_falls_back_to_httpapi_without_upnp(monkeypatch) -> None:
    played: dict = {}

    class _Device:
        def play_url(self, url):
            played["url"] = url

    ctrl = _caster(monkeypatch, None, _Device())
    ctrl.cast("192.168.1.9", "ytmusic", "vid", _META)
    assert played["url"] == "http://relay/tok"
    assert ctrl._relay.registered[-1]["allow_icy"] is True  # ICY best-effort for httpapi


# -- the desktop's active queue (shared PlayQueue model) ------------------------


def _track_n(n: int) -> Track:
    return Track(id=f"t{n}", title=f"Song {n}", service=Service.YTMUSIC, artists=["A"],
                 duration_s=100)


class _FakeLocalPlayer:
    def __init__(self) -> None:
        self.loaded: list[str] = []
        self.stopped = 0

    def load_and_play(self, url, headers):
        self.loaded.append(url)

    def stop(self):
        self.stopped += 1

    def seek(self, pos):
        return True

    def pause(self):
        pass

    def resume(self):
        pass


class _Provider:
    def __init__(self) -> None:
        self.resolved: list[str] = []

    def resolve_stream(self, track_id, *, max_quality=False):
        self.resolved.append(track_id)
        return StreamSource(url=f"http://cdn/{track_id}", mime_type="audio/flac", container="flac")


class _FakeEngine:
    """The engine's device-queue API backed by a REAL DeviceQueues (fake device)."""

    def __init__(self) -> None:
        from harmony.web.device_queue import DeviceQueues

        self.cast: list[tuple[str, str]] = []
        self.controls: list[tuple[str, str, object, object]] = []
        self.queues = DeviceQueues(lambda h, t: self.cast.append((h, t["id"])), lambda h: {})
        self.queues._ensure_poller = lambda host: None
        self.op_vias: list = []

    def device_queue_op(self, host, op, body, via=None):
        self.op_vias.append(via)
        if op == "stop":
            self.queues.stop(host)
            return self.queues.snapshot(host)
        return self.queues.op(host, op, body)

    def device_queue(self, host, via=None):
        return self.queues.snapshot(host)

    def device_control(self, host, action, level=None, via=None):
        self.controls.append((host, action, level, via))
        return {"ok": True}


@pytest.fixture
def player(state: AppState, monkeypatch):
    """A state wired for playback: inline async, a fake local player/provider/engine."""
    monkeypatch.setattr("harmony.ui.state.run_async",
                        lambda fn, done=None, err=None: _inline(fn, done, err))
    monkeypatch.setattr("harmony.ui.state.on_main", lambda fn, *a: fn(*a))
    monkeypatch.setattr("harmony.ui.state.GLib.timeout_add", lambda *a: 1)
    state._local_player = _FakeLocalPlayer()
    prov = _Provider()
    state.providers = {Service.YTMUSIC: prov}
    eng = _FakeEngine()
    monkeypatch.setattr(AppState, "_engine", staticmethod(lambda: eng))
    toasts: list[str] = []
    state.connect("toast", lambda _s, t: toasts.append(t))
    return state, prov, eng, toasts


def _inline(fn, done, err):
    try:
        r = fn()
    except Exception as exc:  # noqa: BLE001
        if err:
            err(exc)
        return
    if done:
        done(r)


def test_play_list_here_queues_the_whole_album_from_the_clicked_track(player) -> None:
    state, prov, _eng, _t = player
    tracks = [_track_n(i) for i in range(1, 5)]
    state.play_list(tracks, 2)
    assert prov.resolved == ["t3"]
    assert [t.id for t in state.active_queue()] == ["t1", "t2", "t3", "t4"]
    assert state.queue_index() == 2 and state.playback.track.id == "t3"
    assert state.playback.has_next and state.playback.has_prev


def test_local_track_end_advances_then_stops_at_the_end(player) -> None:
    state, prov, _eng, _t = player
    state.play_list([_track_n(1), _track_n(2)], 0)
    state._on_local_eos()
    assert prov.resolved == ["t1", "t2"]
    state._on_local_eos()
    assert state.playback.state == "stopped"
    assert [t.id for t in state.active_queue()] == ["t1", "t2"]  # queue kept for replay


def test_repeat_one_replays_on_natural_end_but_next_moves_on(player) -> None:
    state, prov, _eng, _t = player
    state.play_list([_track_n(1), _track_n(2)], 0)
    state.playback_set_repeat("one")
    state._on_local_eos()
    state.playback_next()
    assert prov.resolved == ["t1", "t1", "t2"]


def test_enqueue_while_playing_does_not_interrupt(player) -> None:
    """The audit's bug: enqueue during a single track replaced it."""
    state, prov, _eng, toasts = player
    state.play_list([_track_n(1)], 0)
    state.playback.state = "playing"
    state.playback_enqueue([_track_n(2), _track_n(3)])
    assert prov.resolved == ["t1"]
    assert [t.id for t in state.active_queue()] == ["t1", "t2", "t3"]
    assert toasts[-1] == "Added 2 to the queue"


def test_play_next_then_jump_plays_that_exact_row(player) -> None:
    """The audit's bug: jump-to looked in the original collection and restarted it."""
    state, prov, _eng, _t = player
    state.play_list([_track_n(1), _track_n(2)], 0)
    state.playback.state = "playing"
    state.playback_play_next([_track_n(9)])
    assert [t.id for t in state.active_queue()] == ["t1", "t9", "t2"]
    state.playback_jump(1)
    assert prov.resolved[-1] == "t9" and state.queue_index() == 1


def test_previous_restarts_after_three_seconds_else_goes_back(player) -> None:
    state, prov, _eng, _t = player
    state.play_list([_track_n(1), _track_n(2)], 1)
    state.playback.state = "playing"
    state.playback.position_s = 40
    state.playback_previous()
    assert prov.resolved == ["t2"]            # restarted in place (seek), no re-resolve
    state.playback.position_s = 1
    state.playback_previous()
    assert prov.resolved[-1] == "t1"


def test_remove_and_clear(player) -> None:
    state, _prov, _eng, _t = player
    state.play_list([_track_n(1), _track_n(2), _track_n(3)], 1)
    state.playback.state = "playing"
    state.playback_remove_at(0)
    assert [t.id for t in state.active_queue()] == ["t2", "t3"] and state.queue_index() == 0
    state.playback_clear()
    assert [t.id for t in state.active_queue()] == ["t2"]


def test_a_failed_resolve_skips_but_all_failing_stops(player) -> None:
    state, prov, _eng, toasts = player

    def boom(track_id, *, max_quality=False):
        raise RuntimeError("geo-blocked")

    prov.resolve_stream = boom
    state.play_list([_track_n(1), _track_n(2)], 0)
    assert state.playback.state == "stopped"
    assert sum("geo-blocked" in t for t in toasts) == 2  # tried both, then stopped


def test_device_playback_hands_the_list_to_the_engine_queue(player) -> None:
    state, prov, eng, _t = player
    state.play_list([_track_n(1), _track_n(2), _track_n(3)], 1, host="192.168.1.9")
    assert eng.cast == [("192.168.1.9", "t2")]        # the engine's queue started it
    assert prov.resolved == []                         # nothing resolved locally
    assert [t.id for t in state.active_queue()] == ["t1", "t2", "t3"]
    assert state.queue_index() == 1
    state.playback_next()
    assert eng.cast[-1] == ("192.168.1.9", "t3")


def test_peer_device_goes_via_the_peer(player) -> None:
    state, _prov, eng, _t = player
    state.play_list([_track_n(1)], 0, host="10.0.0.2:8080/192.168.50.7")
    assert eng.cast == [("192.168.50.7", "t1")]
    assert eng.op_vias[-1] == "10.0.0.2:8080"
    state.playback_set_volume(30)
    assert eng.controls[-1] == ("192.168.50.7", "volume", 30, "10.0.0.2:8080")


def test_switching_output_moves_the_whole_queue_in_order(player) -> None:
    state, prov, eng, _t = player
    state.play_list([_track_n(1), _track_n(2), _track_n(3)], 1)   # here, on t2
    state.playback.state = "playing"
    state.playback_set_active_device("192.168.1.9")
    local = state._local_player
    assert local.stopped >= 1                                     # old output stopped
    assert eng.cast == [("192.168.1.9", "t2")]                    # resumes the current track
    assert [t.id for t in state.active_queue()] == ["t1", "t2", "t3"]
    state.playback_set_active_device("__local__")                 # and back
    assert prov.resolved[-1] == "t2"


def test_selecting_a_device_with_nothing_playing_just_switches_view(player) -> None:
    state, _prov, eng, _t = player
    state.playback_set_active_device("192.168.1.9")
    assert state.playback.active_host == "192.168.1.9" and eng.cast == []


def test_split_target() -> None:
    assert AppState.split_target("192.168.1.9") == ("192.168.1.9", None)
    assert AppState.split_target("10.0.0.2:8080/192.168.50.7") == ("192.168.50.7", "10.0.0.2:8080")
