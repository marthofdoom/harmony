"""Saved peers: engine writes must survive the desktop's settings saves, the
federated library must see them at once, and the address helpers must parse."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harmony import config as config_module
from harmony.library_federation import FederatedLibraryProvider, fed_id
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


# -- review findings (1.0.10, second pass) ---------------------------------------

LAN, TS = "192.168.12.70", "100.101.102.103"


def test_collapsed_instance_with_a_saved_address_is_removable() -> None:
    from harmony.mesh import dedupe_instances

    rows = [
        {"name": "box", "host": LAN, "port": 8080, "instance_id": "x", "source": "mdns"},
        {"name": "box", "host": TS, "port": 8080, "instance_id": "x", "source": "manual"},
    ]
    (got,) = dedupe_instances(rows)
    assert got["host"] == LAN                          # preferred address
    assert got["source"] == "both"                     # so the desktop shows Remove
    assert got["saved"] == [{"host": TS, "port": 8080}]
    assert got["addresses"] == [f"{LAN}:8080", f"{TS}:8080"]


def test_remove_peer_by_instance_id_removes_every_saved_address(isolated) -> None:
    config_module.Settings(known_peers=[
        {"host": LAN, "port": 8080, "instance_id": "x"}, {"host": TS, "port": 8080, "instance_id": "x"},
        {"host": "10.0.0.9", "port": 8080, "instance_id": "other"},
    ]).save()
    out = Engine().remove_peer("1.2.3.4", 1, instance_id="x")
    assert out == {"ok": True, "removed": 2}
    assert [p["host"] for p in json.loads(isolated.read_text())["known_peers"]] == ["10.0.0.9"]
    assert Engine().remove_peer()["ok"] is False


def _fed(rows):
    return FederatedLibraryProvider(None, key=lambda: None, peers=lambda: rows)


def test_ids_made_via_any_address_keep_resolving(monkeypatch) -> None:
    rows = [{"name": "box", "host": TS, "port": 8080, "instance_id": "x"}]
    prov = _fed(rows)
    old_id = fed_id("t1", f"{TS}:8080")                # created while only Tailscale was known
    assert prov._route(old_id) == ("t1", f"{TS}:8080")
    rows.append({"name": "box", "host": LAN, "port": 8080, "instance_id": "x"})   # LAN appears
    prov.invalidate()
    assert prov._known() == {f"{LAN}:8080": "box"}      # LAN now preferred ...
    assert prov._route(old_id)[0] == "t1"                # ... and the old id still routes
    assert prov.instance_name(old_id) == "box"
    with pytest.raises(Exception, match="isn't a known instance"):
        prov._route(fed_id("t1", "9.9.9.9:1"))

    import requests

    seen: list[str] = []

    class R:
        ok, status_code = True, 200

        def json(self):
            return {"token": "tok", "mime": "audio/flac"}

    def fake(method, url, **kw):
        seen.append(url)
        return R()

    monkeypatch.setattr(requests, "request", fake)
    src = prov.resolve_stream(old_id)
    assert seen[0].startswith(f"http://{LAN}:8080/")
    assert src.url == f"http://{LAN}:8080/stream/tok"


def test_dead_preferred_address_falls_back_and_is_remembered(monkeypatch) -> None:
    import requests

    prov = _fed([{"name": "box", "host": LAN, "port": 8080, "instance_id": "x"},
                 {"name": "box", "host": TS, "port": 8080, "instance_id": "x"}])
    seen: list[str] = []

    class R:
        ok, status_code = True, 200

        def json(self):
            return {"token": "tok"}

    def fake(method, url, **kw):
        seen.append(url)
        if f"//{LAN}:" in url:
            raise requests.ConnectionError("away from home")
        return R()

    monkeypatch.setattr(requests, "request", fake)
    src = prov.resolve_stream(fed_id("t1", f"{LAN}:8080"))
    assert [u.split("/")[2] for u in seen] == [f"{LAN}:8080", f"{TS}:8080"]
    assert src.url == f"http://{TS}:8080/stream/tok"      # URL handed out uses the address that worked
    seen.clear()
    prov.resolve_stream(fed_id("t1", f"{LAN}:8080"))
    assert [u.split("/")[2] for u in seen] == [f"{TS}:8080"]    # remembered: no re-try of the dead one


def test_known_is_memoized_briefly() -> None:
    calls = []

    def peers():
        calls.append(1)
        return [{"name": "b", "host": LAN, "port": 8080, "instance_id": "x"}]

    prov = FederatedLibraryProvider(None, key=lambda: None, peers=peers)
    for _ in range(5):
        prov._known()
        prov._route(fed_id("a", f"{LAN}:8080"))
    assert len(calls) == 1
    prov.invalidate()
    prov._known()
    assert len(calls) == 2


def test_local_networks_are_cached(monkeypatch) -> None:
    from harmony import mesh

    n = []
    monkeypatch.setattr(mesh, "_nets_cache", None)
    monkeypatch.setattr(mesh, "_enumerate_local_networks", lambda: (n.append(1), [])[1])
    for _ in range(4):
        mesh._local_networks()
    assert len(n) == 1
    mesh._nets_cache = (mesh._nets_cache[0] - 11, [])      # older than the 10 s TTL
    mesh._local_networks()
    assert len(n) == 2


def test_same_instance_id_on_another_server_is_not_us(isolated, serve) -> None:
    engine = Engine()
    engine.set_http_port(8080)
    port = serve({"service": "harmony", "instance_id": engine.instance_id()})
    got = engine.add_peer("127.0.0.1", port)             # loopback, but not OUR port
    assert got["ok"] is False and "same instance id" in got["reason"] and "copied" in got["reason"]
    assert not json.loads(isolated.read_text()).get("known_peers")
    engine.set_http_port(port)                            # now it really is us
    assert engine.add_peer("127.0.0.1", port) == {"ok": False, "reason": "that's this instance"}


def test_mdns_self_drop_needs_the_port_too() -> None:
    from harmony.mesh import Mesh, dedupe_instances

    class Info:
        port = 9999

        def parsed_addresses(self):
            return ["192.168.1.5"]

        properties = {b"iid": b"me", b"name": b"twin"}

    class ZC:
        def get_service_info(self, *_a, **_k):
            return Info()

    m = Mesh("me", 8080, "me")
    m._resolve(ZC(), "t", "twin._harmony._tcp.local.")
    assert [p["port"] for p in m.peers()] == [9999]       # same id, other port: kept
    Info.port = 8080
    m._resolve(ZC(), "t", "again._harmony._tcp.local.")
    assert "again._harmony._tcp.local." not in m._peers
    rows = [{"host": "h", "port": 9999, "instance_id": "me"}, {"host": "h", "port": 8080, "instance_id": "me"}]
    assert [r["port"] for r in dedupe_instances(rows, own_id="me", own_port=8080)] == [9999]


# -- Settings merge / save ------------------------------------------------------


def test_setting_a_field_back_to_its_original_value_beats_disk(isolated) -> None:
    config_module.Settings(last_page="search").save()
    held = config_module.Settings.load()
    other = config_module.Settings.load()
    other.last_page = "library"
    other.save()
    held.last_page = "search"                  # == what held loaded, but deliberately assigned
    held.save()
    assert json.loads(isolated.read_text())["last_page"] == "search"
    untouched = config_module.Settings.load()
    writer = config_module.Settings.load()
    writer.theme = "x"
    writer.save()
    untouched.window_width = 1000
    untouched.save()
    assert json.loads(isolated.read_text())["theme"] == "x"      # disk still wins for unassigned fields


def test_in_place_mutation_beats_disk(isolated) -> None:
    held = config_module.Settings.load()
    other = config_module.Settings.load()
    other.last_page = "library"
    other.save()
    held.known_peers.append({"host": "h", "port": 1})
    held.save()
    saved = json.loads(isolated.read_text())
    assert saved["known_peers"] == [{"host": "h", "port": 1}] and saved["last_page"] == "library"


def test_concurrent_saves_lose_nothing_and_do_not_raise(isolated) -> None:
    config_module.Settings().save()
    errors: list[BaseException] = []

    def work(field: str, value: object) -> None:
        try:
            s = config_module.Settings.load()
            setattr(s, field, value)
            for _ in range(25):
                s.save()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=work, args=("last_page", "a")),
          threading.Thread(target=work, args=("theme", "b"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    saved = json.loads(isolated.read_text())
    assert (saved["last_page"], saved["theme"]) == ("a", "b")
    assert [p.name for p in isolated.parent.iterdir()] == ["settings.json"]     # no stray temp files


def test_unreadable_settings_file_is_never_overwritten(isolated, serve) -> None:
    isolated.write_text("{ this is not json", "utf-8")
    s = config_module.Settings.load()
    assert s._load_failed
    s.last_page = "library"
    s.save()                                               # refused
    assert isolated.read_text() == "{ this is not json"
    assert (isolated.parent / "settings.json.corrupt").read_text() == "{ this is not json"
    engine = Engine()                                      # what an unauthenticated /healthz does
    iid = engine.healthz()["instance_id"]
    assert iid and engine.instance_id() == iid             # stable in memory
    assert isolated.read_text() == "{ this is not json"
    isolated.write_text("", "utf-8")                       # an empty file holds nothing: saving is fine
    config_module.Settings.load().save()
    assert json.loads(isolated.read_text())["last_page"]


def test_a_slow_peer_is_not_retried_on_its_other_addresses(monkeypatch):
    """A read timeout means the server was reached; trying its other address
    only doubles the wait (and re-sends the request)."""
    import pytest
    import requests

    from harmony.library_federation import FederatedLibraryProvider

    calls = []

    def slow(method, url, **kw):
        calls.append(url)
        raise requests.ReadTimeout("slow")

    monkeypatch.setattr(requests, "request", slow)
    prov = FederatedLibraryProvider(None, lambda: [], lambda: None)
    monkeypatch.setattr(prov, "_candidates", lambda peer: ["192.168.1.5:8080", "100.64.1.5:8080"])
    with pytest.raises(requests.ReadTimeout):
        prov._request("GET", "192.168.1.5:8080", "/x", timeout=1)
    assert calls == ["http://192.168.1.5:8080/x"]


def test_blank_settings_file_is_fresh_not_failed(tmp_path, monkeypatch):
    import harmony.config as config

    monkeypatch.setattr(config, "settings_path", lambda: tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text("  \n", "utf-8")
    s = config.Settings.load()
    assert not s._load_failed
    s.last_page = "library"
    s.save()
    assert config.Settings.load().last_page == "library"
