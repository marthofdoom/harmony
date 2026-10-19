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
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"status": "ok", "service": "harmony", "version": "1.0.7"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: object) -> None:
        pass


@pytest.fixture
def healthz_port():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


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


def test_without_listener_the_stale_copy_would_clobber(isolated, healthz_port) -> None:
    """Documents the bug the listener fixes."""
    held = config_module.Settings.load()
    Engine().add_peer("127.0.0.1", healthz_port)
    held.save()
    assert json.loads(isolated.read_text())["known_peers"] == []


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
