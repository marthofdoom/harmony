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
from gi.repository import GObject

from harmony import config as config_module
from harmony.playback import DeviceInfo, WiiMDevice
from harmony.ui.state import AppState


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
