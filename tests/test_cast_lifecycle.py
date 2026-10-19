"""Cast connections are bounded, short-lived, and only for the active output."""

from __future__ import annotations

import threading
import types
from typing import Any

import pytest

from harmony.errors import ProviderError
from harmony.playback import chromecast as cc
from harmony.playback.known import reconcile_known
from harmony.web import device_queue as dqm
from harmony.web.api import Engine
from harmony.web.device_queue import DeviceQueues


class _Cast:
    def __init__(self) -> None:
        self.listeners: list[Any] = []
        self.disconnected = 0
        self.media_controller = types.SimpleNamespace(status=None, update_status=lambda *a, **k: None)
        self.status = None

    def register_connection_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def wait(self, timeout: float | None = None) -> None:
        pass

    def disconnect(self, timeout: float | None = None) -> None:
        self.disconnected += 1


@pytest.fixture(autouse=True)
def _clean():
    cc._CONNECTIONS.clear()
    cc._LAST_USED.clear()
    cc.set_busy_provider(None)
    yield
    cc._CONNECTIONS.clear()
    cc.set_busy_provider(None)


@pytest.fixture
def fake_pc(monkeypatch):
    calls: list[dict] = []
    casts: list[_Cast] = []

    def chromecast(info, tries=None, retry_wait=None, timeout=None):
        calls.append({"host": info.host, "cast_type": info.cast_type, "tries": tries,
                      "retry_wait": retry_wait, "timeout": timeout})
        c = _Cast()
        casts.append(c)
        return c

    models = types.SimpleNamespace(
        HostServiceInfo=lambda host, port: (host, port),
        CastInfo=lambda services, uuid, model, name, host, port, cast_type, mfr: types.SimpleNamespace(
            services=services, uuid=uuid, host=host, port=port, cast_type=cast_type))
    monkeypatch.setattr(cc, "_pychromecast",
                        lambda: types.SimpleNamespace(Chromecast=chromecast, models=models))
    monkeypatch.setattr(cc, "_ensure_reaper", lambda: None)
    return calls, casts


def test_connect_is_bounded(fake_pc) -> None:
    calls, _ = fake_pc
    cc._connect("10.0.0.5")
    assert calls[0]["tries"] == cc.CONNECT_TRIES and calls[0]["tries"] is not None
    assert calls[0]["retry_wait"] == cc.CONNECT_RETRY_WAIT_S
    assert calls[0]["timeout"] == cc.CONNECT_TIMEOUT_S


@pytest.mark.parametrize("status", ["LOST", "FAILED", "DISCONNECTED"])
def test_lost_connection_is_forgotten_and_disconnected(fake_pc, status) -> None:
    _, casts = fake_pc
    cc._connect("10.0.0.5")
    assert "10.0.0.5" in cc._CONNECTIONS
    casts[0].listeners[0].new_connection_status(types.SimpleNamespace(status=status))
    assert "10.0.0.5" not in cc._CONNECTIONS
    assert casts[0].disconnected == 1
    cc._connect("10.0.0.5")  # next real use reconnects fresh
    assert len(casts) == 2


def test_connected_status_is_ignored(fake_pc) -> None:
    _, casts = fake_pc
    cc._connect("10.0.0.5")
    casts[0].listeners[0].new_connection_status(types.SimpleNamespace(status="CONNECTED"))
    assert "10.0.0.5" in cc._CONNECTIONS


def test_failed_while_connecting_raises_and_disconnects(monkeypatch, fake_pc) -> None:
    _, casts = fake_pc
    orig = _Cast.wait

    def wait(self, timeout=None):
        self.listeners[0].new_connection_status(types.SimpleNamespace(status="FAILED"))

    monkeypatch.setattr(_Cast, "wait", wait)
    with pytest.raises(ProviderError):
        cc._connect("10.0.0.5")
    assert "10.0.0.5" not in cc._CONNECTIONS and casts[0].disconnected == 1
    monkeypatch.setattr(_Cast, "wait", orig)


def test_idle_reaper_closes_unused_and_skips_busy(fake_pc) -> None:
    _, casts = fake_pc
    cc._connect("10.0.0.5")
    cc._connect("10.0.0.6")
    cc.set_busy_provider(lambda: {"10.0.0.6"})
    later = cc._LAST_USED["10.0.0.5"] + cc.IDLE_CLOSE_S + 1
    assert cc._reap_idle(now=later) == ["10.0.0.5"]
    assert "10.0.0.5" not in cc._CONNECTIONS and casts[0].disconnected == 1
    assert "10.0.0.6" in cc._CONNECTIONS and casts[1].disconnected == 0
    assert cc._reap_idle(now=later - cc.IDLE_CLOSE_S) == []  # recently used is kept


def test_release_closes_unless_active(fake_pc) -> None:
    _, casts = fake_pc
    cc._connect("a")
    cc._connect("b")
    cc.set_busy_provider(lambda: {"b"})
    assert cc.release("a") is True and cc.release("b") is False
    assert "a" not in cc._CONNECTIONS and "b" in cc._CONNECTIONS


def test_passive_status_does_not_count_as_use(fake_pc) -> None:
    cc._connect("10.0.0.5")
    t0 = cc._LAST_USED["10.0.0.5"] = 1.0
    dev = cc.ChromecastDevice("10.0.0.5")
    dev.status(connect=False)
    assert cc._LAST_USED["10.0.0.5"] == t0
    dev.status()
    assert cc._LAST_USED["10.0.0.5"] > t0


# -- device queue ------------------------------------------------------------

def _tracks(n: int) -> list[dict]:
    return [{"service": "s", "id": str(i), "title": f"t{i}"} for i in range(n)]


def test_three_failures_halt_the_queue(monkeypatch) -> None:
    attempts: list[str] = []
    released: list[str] = []

    def play(host, t):
        attempts.append(t["id"])
        raise ProviderError("couldn't connect")

    qs = DeviceQueues(play, lambda h: {}, on_inactive=released.append)
    snap = qs.op("tv", "load", {"tracks": _tracks(10), "start": 0})
    assert len(attempts) == dqm.MAX_START_FAILURES == 3
    assert snap["playing"] is False
    assert snap["error"] == "Couldn't start 3 tracks in a row on tv — stopped"
    assert released == ["tv"]
    qs.op("tv", "jump", {"index": 5})  # a user action gets a fresh run
    assert len(attempts) == 6


def test_single_failure_keeps_real_error() -> None:
    qs = DeviceQueues(lambda h, t: (_ for _ in ()).throw(ProviderError("boom")), lambda h: {})
    snap = qs.op("tv", "load", {"tracks": _tracks(1), "start": 0})
    assert snap["playing"] is False and "boom" in snap["error"]


def test_stop_releases_the_host() -> None:
    released: list[str] = []
    qs = DeviceQueues(lambda h, t: {}, lambda h: {}, on_inactive=released.append)
    qs.op("tv", "load", {"tracks": _tracks(2), "start": 0})
    assert qs.busy_hosts() == {"tv"} and released == []
    qs.op("tv", "stop", {})
    assert qs.busy_hosts() == set() and released == ["tv"]


# -- engine: nothing talks to a non-active cast device -----------------------

class _SpyCaster:
    def __init__(self) -> None:
        self.status_connect: list[bool] = []
        self.controls: list[str] = []

    def status(self, host, kind="wiim", device_info=None, *, connect=True):
        self.status_connect.append(connect)
        return {"state": "stopped"}

    def control(self, host, action, level=None, kind="wiim", device_info=None):
        self.controls.append(action)
        return {"ok": True}

    def cast(self, *a, **k):
        return {"ok": True}


@pytest.fixture
def engine(monkeypatch):
    e = Engine()
    spy = _SpyCaster()
    monkeypatch.setattr(e, "_caster", lambda: spy)
    monkeypatch.setattr(e, "_device_kind", lambda host: ("cast", {"name": "TV"}))
    e.spy = spy
    return e


def test_inactive_cast_is_never_connected_by_any_poll(engine) -> None:
    engine.device_status("10.0.0.5")                  # /api/devices/<h>/status
    engine.device_status("10.0.0.5", connect=True)
    engine.device_queue("10.0.0.5")                   # idle queue poll
    assert engine.spy.status_connect and not any(engine.spy.status_connect)


def test_inactive_cast_refuses_commands(engine) -> None:
    for action in ("pause", "resume", "volume", "seek"):
        with pytest.raises(ProviderError):
            engine.device_control("10.0.0.5", action, 5)
    assert engine.spy.controls == []


def test_play_to_it_connects_and_stop_disconnects(engine, monkeypatch) -> None:
    released: list[str] = []
    monkeypatch.setattr(cc, "release", released.append)
    engine._device_queues().op("10.0.0.5", "load", {"tracks": _tracks(2), "start": 0})
    assert "10.0.0.5" in engine._active_hosts()
    engine.device_status("10.0.0.5")
    assert engine.spy.status_connect[-1] is True      # active: may connect
    engine.device_control("10.0.0.5", "pause")        # commands allowed while active
    assert engine.spy.controls == ["pause"]
    engine.device_queue_op("10.0.0.5", "stop", {})
    assert "10.0.0.5" not in engine._active_hosts()
    assert "10.0.0.5" in released                     # disconnected at once
    engine.device_status("10.0.0.5")
    assert engine.spy.status_connect[-1] is False     # and passive again


def test_direct_cast_is_a_one_track_queue_and_releases_on_end(engine, monkeypatch) -> None:
    released: list[str] = []
    monkeypatch.setattr(cc, "release", released.append)
    monkeypatch.setattr(engine, "_cast_direct", lambda *a, **k: {"ok": True})
    engine.cast("10.0.0.5", "s", "1")
    assert "10.0.0.5" in engine._active_hosts()
    snap = engine._device_queues().snapshot("10.0.0.5")
    assert len(snap["tracks"]) == 1
    # the queue's end detection applies: the track finishing releases the device
    q = engine._device_queues()
    q.after_status("10.0.0.5", {"state": "playing", "position_s": 10, "duration_s": 100})
    q._queues["10.0.0.5"].settle_until = 0
    q.after_status("10.0.0.5", {"state": "playing", "position_s": 10, "duration_s": 100})
    q.after_status("10.0.0.5", {"state": "playing", "position_s": 99, "duration_s": 100})
    assert "10.0.0.5" not in engine._active_hosts()


def test_direct_cast_error_reaches_the_caller(engine, monkeypatch) -> None:
    def boom(*a, **k):
        raise ProviderError("no route")

    monkeypatch.setattr(engine, "_cast_direct", boom)
    with pytest.raises(ProviderError, match="no route"):
        engine.cast("10.0.0.5", "s", "1")
    assert engine._active_hosts() == set()


def test_stop_to_inactive_cast_never_connects(engine, monkeypatch) -> None:
    monkeypatch.setattr(cc, "has_connection", lambda h: False)
    engine.device_control("10.0.0.5", "stop")
    engine.device_queue_op("10.0.0.5", "stop", {})
    assert engine.spy.controls == []


def test_stop_to_active_cast_reaches_the_device(engine, monkeypatch) -> None:
    monkeypatch.setattr(cc, "release", lambda h: None)
    engine._device_queues().op("10.0.0.5", "load", {"tracks": _tracks(2), "start": 0})
    engine.device_queue_op("10.0.0.5", "stop", {})
    assert engine.spy.controls == ["stop"]


def test_stop_to_inactive_cast_with_leftover_connection_closes_it(engine, monkeypatch) -> None:
    monkeypatch.setattr(cc, "has_connection", lambda h: True)
    engine.device_control("10.0.0.5", "stop")
    assert engine.spy.controls == ["stop"]


def test_device_stop_without_connection_is_a_noop(fake_pc) -> None:
    _, casts = fake_pc
    cc.ChromecastDevice("10.0.0.5").stop()
    assert casts == []  # never connected just to stop


def test_connect_passes_cast_type_so_no_eureka_fetch(fake_pc) -> None:
    calls, _ = fake_pc
    cc._connect("10.0.0.5")
    cc._connect("10.0.0.6", cast_type="audio")
    assert [c["cast_type"] for c in calls] == ["cast", "audio"]


def test_hosts_connect_independently(fake_pc, monkeypatch) -> None:
    _, casts = fake_pc
    gate = threading.Event()
    entered = threading.Event()
    orig = _Cast.wait

    def wait(self, timeout=None):
        if len(casts) == 1:  # the first (dead) host hangs
            entered.set()
            gate.wait(5)
        orig(self, timeout)

    monkeypatch.setattr(_Cast, "wait", wait)
    t = threading.Thread(target=lambda: cc._connect("dead"), daemon=True)
    t.start()
    assert entered.wait(2)
    cc._connect("alive")  # would deadlock under one global connect lock
    assert "alive" in cc._CONNECTIONS
    gate.set()
    t.join(3)


def test_dead_cached_connection_is_evicted(fake_pc) -> None:
    _, casts = fake_pc
    cc._connect("h")
    casts[0].socket_client = types.SimpleNamespace(stop=threading.Event(), is_alive=lambda: True)
    casts[0].socket_client.stop.set()
    cc._connect("h")
    assert len(casts) == 2 and casts[0].disconnected == 1


def test_dead_thread_is_evicted(fake_pc) -> None:
    _, casts = fake_pc
    cc._connect("h")
    casts[0].socket_client = types.SimpleNamespace(stop=threading.Event(), is_alive=lambda: False)
    assert cc.has_connection("h") is False


def test_failed_then_connected_retry_succeeds(fake_pc, monkeypatch) -> None:
    def wait(self, timeout=None):
        self.listeners[0].new_connection_status(types.SimpleNamespace(status="FAILED"))
        self.listeners[0].new_connection_status(types.SimpleNamespace(status="CONNECTED"))

    monkeypatch.setattr(_Cast, "wait", wait)
    assert cc._connect("h") is not None and "h" in cc._CONNECTIONS


def test_status_passive_never_connects(fake_pc) -> None:
    _, casts = fake_pc
    st = cc.ChromecastDevice("h").status(connect=False)
    assert st.state == "stopped" and casts == []


def test_discovery_uuid_none_is_blank() -> None:
    import sys

    class CI:
        uuid = None
        host = "1.2.3.4"
        friendly_name = "TV"
        model_name = None
        port = 8009
        cast_type = "cast"

    class Browser:
        devices = {"a": CI()}

        def __init__(self, *a): ...
        def start_discovery(self): ...
        def stop_discovery(self): ...

    zc = types.SimpleNamespace(Zeroconf=lambda: types.SimpleNamespace(close=lambda: None))
    disc = types.SimpleNamespace(CastBrowser=Browser, SimpleCastListener=lambda: None)
    sys.modules["zeroconf"] = zc
    sys.modules["pychromecast.discovery"] = disc
    try:
        import pytest as _p

        mp = _p.MonkeyPatch()
        mp.setattr(cc, "_pychromecast", lambda: object())
        mp.setattr("time.sleep", lambda s: None)
        out = cc.discover_cast(timeout=0)
        mp.undo()
    finally:
        sys.modules.pop("zeroconf", None)
        sys.modules.pop("pychromecast.discovery", None)
    assert out and out[0].raw["uuid"] == "" and out[0].id == "1.2.3.4"


# -- uuid dedupe + stale address ---------------------------------------------

def test_reconcile_follows_uuid_to_new_host() -> None:
    known = [{"host": "192.168.12.123", "name": "TV", "kind": "cast", "uuid": "u1"}]
    assert reconcile_known(known, [{"host": "192.168.12.100", "uuid": "u1"}]) is True
    assert known[0]["host"] == "192.168.12.100"


def test_reconcile_stores_missing_uuid_and_drops_duplicate_host() -> None:
    known = [{"host": "h1", "kind": "cast"}, {"host": "h0", "kind": "cast", "uuid": "u1"}]
    reconcile_known(known, [{"host": "h1", "uuid": "u1"}])
    assert known == [{"host": "h1", "kind": "cast", "uuid": "u1"}]


def test_engine_devices_dedupes_by_uuid_and_updates_saved_host(monkeypatch) -> None:
    from harmony.config import Settings

    saved = Settings()
    saved.known_devices = [{"host": "192.168.12.123", "name": "TV", "kind": "cast", "uuid": "u1"}]
    writes: list[Any] = []
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: saved))
    monkeypatch.setattr(Settings, "save", lambda self, *a, **k: writes.append(list(self.known_devices)))
    e = Engine()
    found = [{"host": "192.168.12.100", "name": "TV", "kind": "cast", "source": "discovered", "uuid": "u1"},
             {"host": "192.168.12.101", "name": "TV", "kind": "cast", "source": "discovered", "uuid": "u1"},
             {"host": "192.168.12.9", "name": "Kitchen", "kind": "cast", "source": "discovered", "uuid": "u2"}]
    monkeypatch.setattr(e, "_discovered_devices", lambda refresh=False: found)
    hosts = [d["host"] for d in e.devices()["devices"]]
    assert hosts == ["192.168.12.100", "192.168.12.9"]
    assert writes and writes[0][0]["host"] == "192.168.12.100"


def test_desktop_state_dedupes_by_uuid_and_updates_host() -> None:
    from harmony.playback import DeviceInfo
    from harmony.ui.state import AppState

    st = AppState.__new__(AppState)
    st.settings = types.SimpleNamespace(
        known_devices=[{"host": "192.168.12.123", "name": "TV", "kind": "cast", "uuid": "u1"}],
        save=lambda: None)
    st.emit = lambda *a, **k: None
    st.set_discovered_devices([
        DeviceInfo(id="u1", name="TV", host="192.168.12.100", kind="cast", raw={"uuid": "u1"})])
    assert [d.host for d in st.all_devices()] == ["192.168.12.100"]
    assert st.settings.known_devices[0]["host"] == "192.168.12.100"




def test_reconcile_never_crosses_kinds_or_drops_a_different_device() -> None:
    known = [{"host": ".9", "name": "WiiM", "kind": "wiim"},
             {"host": ".5", "name": "TV", "kind": "cast", "uuid": "U1"}]
    reconcile_known(known, [{"host": ".9", "kind": "cast", "uuid": "U1"}])
    wiim = next(k for k in known if k["kind"] == "wiim")
    tv = next(k for k in known if k["kind"] == "cast")
    assert len(known) == 2 and "uuid" not in wiim and tv["host"] == ".9"


def test_reconcile_dedupe_keeps_the_discovery_confirmed_entry() -> None:
    known = [{"host": "old", "name": "A", "kind": "cast", "uuid": "U1"},
             {"host": "new", "name": "B", "kind": "cast", "uuid": "U1"}]
    reconcile_known(known, [{"host": "new", "kind": "cast", "uuid": "U1"}])
    assert known == [{"host": "new", "name": "B", "kind": "cast", "uuid": "U1"}]


# -- queue: lost devices, next past end, idle pause --------------------------

def test_vanished_device_halts_after_three_poll_failures(monkeypatch) -> None:
    monkeypatch.setattr(dqm, "POLL_S", 0.01)
    released: list[str] = []
    polls = []

    def status(h):
        polls.append(h)
        raise ProviderError("timeout")

    qs = DeviceQueues(lambda h, t: {}, status, on_inactive=released.append)
    qs.op("tv", "load", {"tracks": _tracks(2), "start": 0})
    for _ in range(200):
        if released:
            break
        threading.Event().wait(0.02)
    snap = qs.snapshot("tv")
    assert released == ["tv"] and len(polls) == dqm.MAX_POLL_FAILURES
    assert snap["playing"] is False and snap["error"] == "Lost the connection to tv — stopped"


def test_next_past_end_stops_before_releasing() -> None:
    order: list[str] = []
    qs = DeviceQueues(lambda h, t: {}, lambda h: {}, stop=lambda h: order.append("stop"),
                      on_inactive=lambda h: order.append("release"))
    qs.op("tv", "load", {"tracks": _tracks(1), "start": 0})
    qs.op("tv", "next", {})
    assert order == ["stop", "release"]


def test_long_pause_releases_the_device(monkeypatch) -> None:
    monkeypatch.setattr(dqm, "POLL_S", 0.01)
    monkeypatch.setattr(dqm, "PAUSE_TIMEOUT_S", 0.05)
    released: list[str] = []
    qs = DeviceQueues(lambda h, t: {}, lambda h: {"state": "paused"}, stop=lambda h: None,
                      on_inactive=released.append)
    qs.op("tv", "load", {"tracks": _tracks(2), "start": 0})
    qs.note_control("tv", "pause")
    for _ in range(200):
        if released:
            break
        threading.Event().wait(0.02)
    snap = qs.snapshot("tv")
    assert released == ["tv"] and snap["playing"] is False and snap["state"] == "stopped"
