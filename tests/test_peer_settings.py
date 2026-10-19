"""Saved peers: engine writes must survive the desktop's settings saves, the
federated library must see them at once, and the address helpers must parse."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harmony import config as config_module
from harmony.library_federation import FederatedLibraryProvider
from harmony.ui.instances import needs_update, parse_address
from harmony.web.api import Engine


class _Health(BaseHTTPRequestHandler):
    payload: dict = {"status": "ok", "service": "harmony", "version": "1.0.7"}
    code = 200

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(self.payload).encode()
        self.send_response(self.code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: object) -> None:
        pass


def _serve(payload: dict, code: int = 200):
    handler = type("H", (_Health,), {"payload": payload, "code": code})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture
def healthz_port():
    httpd = _serve(_Health.payload)
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def serve():
    started = []

    def make(payload: dict, code: int = 200) -> int:
        httpd = _serve(payload, code)
        started.append(httpd)
        return httpd.server_address[1]

    yield make
    for h in started:
        h.shutdown()
        h.server_close()


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")
    return tmp_path / "settings.json"


def test_engine_add_peer_survives_desktop_save(isolated, healthz_port) -> None:
    pytest.importorskip("gi")
    from gi.repository import GObject

    from harmony.ui.state import AppState

    state = AppState.__new__(AppState)
    GObject.Object.__init__(state)
    state.settings = config_module.Settings.load()   # the desktop's long-lived copy
    engine = Engine()
    engine.add_settings_listener(state._on_engine_settings_changed)
    seen: list[str] = []
    state.connect("devices-changed", lambda *_a: seen.append("devices"))

    assert engine.add_peer("127.0.0.1", healthz_port)["ok"]
    assert state.settings.known_peers[0]["port"] == healthz_port
    assert seen == ["devices"]

    state.settings.last_page = "library"      # what a page switch does
    state.settings.save()
    saved = json.loads(isolated.read_text())
    assert [p["port"] for p in saved["known_peers"]] == [healthz_port]

    engine.remove_peer("127.0.0.1", healthz_port)
    state.settings.save()
    assert json.loads(isolated.read_text())["known_peers"] == []


def test_stale_copy_does_not_clobber_fields_it_never_changed(isolated, healthz_port) -> None:
    """Even with no listener: a held copy saving ITS change keeps what another
    writer (the engine) put on disk meanwhile; fields it changed itself win."""
    held = config_module.Settings.load()
    engine = Engine()
    engine.add_peer("127.0.0.1", healthz_port)
    iid = engine.instance_id()
    held.last_page = "library"
    held.save()
    saved = json.loads(isolated.read_text())
    assert [p["port"] for p in saved["known_peers"]] == [healthz_port]
    assert saved["last_page"] == "library"
    assert [p["port"] for p in held.known_peers] == [healthz_port]      # and in memory
    assert saved["instance_id"] == iid                                       # engine's id survived too


def test_own_change_beats_disk(isolated, healthz_port) -> None:
    held = config_module.Settings.load()
    Engine().add_peer("127.0.0.1", healthz_port)
    held.known_peers = [{"host": "h", "port": 1, "name": "mine"}]   # the holder's own edit
    held.save()
    assert json.loads(isolated.read_text())["known_peers"] == [{"host": "h", "port": 1, "name": "mine"}]


def test_instances_include_saved_peers(isolated, healthz_port) -> None:
    engine = Engine()
    engine.add_peer("127.0.0.1", healthz_port)
    got = engine.instances()["instances"]
    assert [(i["host"], i["port"], i["source"]) for i in got] == [("127.0.0.1", healthz_port, "manual")]


def test_add_peer_invalidates_the_library_probe_cache(isolated, healthz_port) -> None:
    engine = Engine()
    prov = FederatedLibraryProvider(None, peers=lambda: engine.instances()["instances"], key=lambda: None)
    engine._libprov = prov
    peer = f"127.0.0.1:{healthz_port}"
    prov._probe[peer] = (1e18, None)           # a fresh "no library" verdict
    engine.add_peer("127.0.0.1", healthz_port)
    assert prov._probe == {}
    prov._probe[peer] = (1e18, None)
    engine.remove_peer("127.0.0.1", healthz_port)
    assert prov._probe == {}


def test_add_peer_unreachable_is_refused(isolated) -> None:
    assert Engine().add_peer("127.0.0.1", 1)["ok"] is False


def test_add_peer_refuses_a_non_harmony_host(isolated, serve) -> None:
    for payload, code in (({"status": "ok"}, 200), ({"service": "other"}, 200),
                          ({"service": "harmony"}, 503), ({}, 404)):
        port = serve(payload, code)
        got = Engine().add_peer("127.0.0.1", port)
        assert got == {"ok": False, "reason": "not a Harmony server"}, (payload, code)
    assert not isolated.exists() or json.loads(isolated.read_text()).get("known_peers", []) == []


def test_add_peer_refuses_ourselves(isolated, serve) -> None:
    engine = Engine()
    port = serve({"status": "ok", "service": "harmony", "instance_id": engine.instance_id()})
    assert engine.add_peer("127.0.0.1", port) == {"ok": False, "reason": "that's this instance"}


def test_instance_id_is_stable_and_persisted(isolated) -> None:
    a = Engine().instance_id()
    assert a and Engine().instance_id() == a
    assert json.loads(isolated.read_text())["instance_id"] == a
    assert Engine().healthz()["instance_id"] == a


def Settings_peers(path) -> list:
    return json.loads(path.read_text())["known_peers"]


def test_add_peer_stores_the_id_and_a_second_address_does_not_duplicate(isolated, serve) -> None:
    engine = Engine()
    p1 = serve({"service": "harmony", "instance_id": "srv-1", "name": "box"})
    p2 = serve({"service": "harmony", "instance_id": "srv-1", "name": "box"})
    assert engine.add_peer("127.0.0.1", p1)["ok"]
    assert engine.add_peer("localhost", p2)["ok"]          # same server, a hostname
    got = engine.instances()["instances"]
    assert len(got) == 1 and got[0]["instance_id"] == "srv-1"
    assert len(Settings_peers(isolated)) == 1               # stored once, too


def test_instances_dedupe_by_id_preferring_lan_and_dropping_self() -> None:
    from harmony.mesh import dedupe_instances

    rows = [
        {"name": "box", "host": "100.101.102.103", "port": 8080, "instance_id": "x"},   # Tailscale
        {"name": "box", "host": "192.168.1.9", "port": 8080, "instance_id": "x"},       # LAN
        {"name": "old", "host": "10.0.0.5", "port": 8080},
        {"name": "old", "host": "10.0.0.5", "port": 8080},                              # no id: host:port
        {"name": "me", "host": "192.168.1.2", "port": 8080, "instance_id": "me"},
    ]
    got = dedupe_instances(rows, own_id="me")
    assert [(r["host"], r.get("instance_id")) for r in got] == [("192.168.1.9", "x"), ("10.0.0.5", None)]


def test_federated_known_dedupes_by_instance_id() -> None:
    prov = FederatedLibraryProvider(None, key=lambda: None, peers=lambda: [
        {"name": "box", "host": "100.100.1.1", "port": 8080, "instance_id": "x"},
        {"name": "box", "host": "192.168.1.9", "port": 8080, "instance_id": "x"},
    ])
    assert prov._known() == {"192.168.1.9:8080": "box"}


def test_add_peer_brackets_ipv6(isolated, monkeypatch) -> None:
    import requests

    seen: list[str] = []

    class R:
        ok = True

        def json(self):
            return {"service": "harmony", "instance_id": "v6"}

    monkeypatch.setattr(requests, "get", lambda url, **kw: (seen.append(url), R())[1])
    assert Engine().add_peer("fd00::1", 8080)["ok"]
    assert seen == ["http://[fd00::1]:8080/healthz"]


def test_parse_address() -> None:
    assert parse_address("192.168.12.70") == ("192.168.12.70", 8080)
    assert parse_address(" http://srv:8811/ ") == ("srv", 8811)
    assert parse_address("[::1]:9") == ("::1", 9)
    for bad in ("", "  ", "srv:x", "srv:0"):
        with pytest.raises(ValueError):
            parse_address(bad)


def test_needs_update() -> None:
    assert needs_update("1.0.7", "1.0.9")
    assert not needs_update("1.0.9", "1.0.9") and not needs_update("1.0.10", "1.0.9")
    assert not needs_update(None, "1.0.9")
