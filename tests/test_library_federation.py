"""Federated library: one instance plays another instance's library over the mesh.

Two genuinely separate instances: a real ``harmony serve`` SUBPROCESS (its own
config/data dirs, its own library, a personal key) plays the server, and an
in-process instance with NO library of its own, which knows the server only as a
saved peer. Everything the second instance shows and plays comes from the first.
Offline: loopback only.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from test_library import make_library

from harmony.library_federation import FederatedLibraryProvider, fed_id, split_id

KEY = "same-key-everywhere"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _req(url: str, *, data: dict | None = None, headers: dict | None = None, raw: bool = False):
    h = {"X-Harmony-Key": KEY, **(headers or {})}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - loopback test servers
        payload = r.read()
        return (r.status, dict(r.headers), payload) if raw else json.loads(payload)


@pytest.fixture(scope="module")
def server_instance(tmp_path_factory):
    """A real `harmony serve` process with its own library and personal key."""
    root = tmp_path_factory.mktemp("server")
    music = root / "music"
    files = make_library(music)
    cfg = root / "config" / "harmony"
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(json.dumps({
        "library_enabled": True, "library_paths": [str(music)], "personal_key": KEY,
        "musicbrainz_enabled": False}))
    port = _free_port()
    env = {**os.environ, "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
           "XDG_CACHE_HOME": str(root / "cache"), "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
    proc = subprocess.Popen([sys.executable, "-m", "harmony", "serve", "--address", "127.0.0.1",
                             "--port", str(port)], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/healthz", timeout=1).read()  # noqa: S310
                break
            except OSError:
                time.sleep(0.1)
        _req(base + "/api/library/scan", data={})
        for _ in range(100):
            st = _req(base + "/api/library")
            if st["stats"]["tracks"] == 5 and not st["scan"].get("running"):
                break
            time.sleep(0.1)
        assert st["stats"]["tracks"] == 5
        yield SimpleNamespace(url=base, port=port, peer=f"127.0.0.1:{port}", files=files)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def client_instance(server_instance, tmp_path, monkeypatch):
    """An in-process instance with no library, knowing the server as a saved peer."""
    from harmony import config
    from harmony.config import Settings
    from harmony.web import server as srv
    from harmony.web.api import Engine

    (tmp_path / "c").mkdir()
    (tmp_path / "d").mkdir()
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "c")
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "d")
    import harmony.providers as providers
    monkeypatch.setattr(providers, "build_providers", lambda *a, **k: {})
    s = Settings()
    s.personal_key = KEY
    s.musicbrainz_enabled = False
    s.known_peers = [{"host": "127.0.0.1", "port": server_instance.port, "name": "server"}]
    s.save()
    engine = Engine()
    monkeypatch.setattr(srv, "_engine", engine)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.HarmonyHTTPRequestHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    engine.set_http_port(httpd.server_address[1])
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{httpd.server_address[1]}", engine=engine)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_ids_route_to_peers():
    assert fed_id("abc", "10.0.0.2:8080") == "abc@10.0.0.2:8080"
    assert split_id("abc@10.0.0.2:8080") == ("abc", "10.0.0.2:8080")
    assert split_id("abc@[fd7a::1]:8080") == ("abc", "[fd7a::1]:8080")
    assert split_id("abc") == ("abc", None)


def test_client_sees_the_servers_library(server_instance, client_instance):
    c, s = client_instance, server_instance
    st = _req(c.url + "/api/library")
    assert st["enabled"] is False                                 # no library of its own…
    assert [(p["peer"], p["stats"]["tracks"]) for p in st["peers"]] == [(s.peer, 5)]  # …but the server's
    accounts = _req(c.url + "/api/accounts")["accounts"]
    assert {"service": "local", "authenticated": True, "account": "5 tracks · 1 shared",
            "stale": False} in accounts

    browse = _req(c.url + "/api/library/browse?order=title")
    assert browse["enabled"] and [a["title"] for a in browse["albums"]] == ["Homogenic", "Once"]
    once = browse["albums"][1]
    assert once["id"].endswith("@" + s.peer) and once["service"] == "local"
    assert once["artwork_url"].startswith(s.url + "/art/")
    status, headers, art = _req(once["artwork_url"], raw=True, headers={"X-Harmony-Key": ""})
    assert art == b"\xff\xd8\xff-jpeg-cover"                     # key-free cover from the server

    smart = _req(c.url + "/api/search/smart?q=nightwish&service=local")
    assert smart["artist"]["ref"]["id"].endswith("@" + s.peer)
    assert [a["title"] for a in smart["artist"]["albums"]] == ["Once"]

    page = _req(c.url + f"/api/album/local/{urllib.parse.quote(once['id'], safe='')}")
    assert [t["track_number"] for t in page["tracks"]] == [1, 2, 4]
    assert page["artist_ref"]["id"].endswith("@" + s.peer)
    artist = _req(c.url + f"/api/artist/local/{urllib.parse.quote(page['artist_ref']['id'], safe='')}")
    assert [a["title"] for a in artist["albums"]] == ["Once"] and len(artist["top_tracks"]) == 3


def test_client_plays_a_track_from_the_servers_library(server_instance, client_instance):
    c, s = client_instance, server_instance
    once = next(a for a in _req(c.url + "/api/library/browse")["albums"] if a["title"] == "Once")
    page = _req(c.url + f"/api/album/local/{urllib.parse.quote(once['id'], safe='')}")
    nemo = page["tracks"][2]
    r = _req(c.url + f"/api/resolve?service=local&id={urllib.parse.quote(nemo['id'], safe='')}")
    assert r["mime"] == "audio/wav" and r["label"].endswith("· server")
    data = s.files["nemo"].read_bytes()
    # The client's own /stream proxies the server's stream (our key forwarded).
    status, headers, body = _req(c.url + f"/stream/{r['token']}", raw=True, headers={"Range": "bytes=10-49"})
    assert status == 206 and body == data[10:50]
    status, _, body = _req(c.url + f"/stream/{r['token']}", raw=True)
    assert body == data


def test_unknown_peer_ids_are_refused_not_fetched(client_instance):
    bogus = urllib.parse.quote("x@10.9.9.9:1", safe="")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(client_instance.url + f"/api/resolve?service=local&id={bogus}")
    assert b"isn't a known instance" in exc.value.read()
    # Pages are best-effort: refused quietly (empty), never fetched from that host.
    assert _req(client_instance.url + f"/api/album/local/{bogus}")["tracks"] == []


def test_a_peer_with_another_key_shares_nothing(server_instance):
    prov = FederatedLibraryProvider(None, peers=lambda: [{"host": "127.0.0.1", "port": server_instance.port}],
                                    key=lambda: "wrong-key")
    assert prov.library_peers() == [] and not prov.active()
    assert prov.search("nightwish", kinds=("tracks",)).tracks == []


def test_peer_endpoints_never_federate(server_instance):
    # The server's own-library endpoints answer from its own index only (no
    # "@peer" ids), so two instances federating with each other can't recurse.
    r = _req(server_instance.url + "/api/library/own/search?q=once&kinds=albums,tracks")
    assert r["albums"] and all("@" not in a["id"] for a in r["albums"] + r["tracks"])


def test_own_tracks_are_readdressed_for_a_peer_speaker(client_instance):
    eng = client_instance.engine
    out = eng._library_for_peer([
        {"service": "local", "id": "t1", "art_url": "/art/al.sig"},
        {"service": "local", "id": "t2@10.0.0.7:8080", "art_url": "http://10.0.0.7:8080/art/x.y"},
        {"service": "qobuz", "id": "123"}], "127.0.0.1:9")
    me = f"127.0.0.1:{eng._http_port}"
    assert out[0] == {"service": "local", "id": f"t1@{me}", "art_url": f"http://{me}/art/al.sig"}
    assert out[1]["id"] == "t2@10.0.0.7:8080" and out[2] == {"service": "qobuz", "id": "123"}


def test_a_peers_library_copy_plays_instead_of_the_stream(server_instance, client_instance, monkeypatch):
    """A song found on Qobuz on an instance with no library plays from the
    SERVER's library: the row is marked, and resolve streams the server's file."""
    from conftest import FakeProvider

    import harmony.providers as providers
    from harmony.models import Service, Track

    q = FakeProvider(Service.QOBUZ, [Track(id="q-nemo", title="Nemo", service=Service.QOBUZ,
                                           artists=["Nightwish"], album="Once")])
    q.is_authenticated = True
    q.resolve_stream = lambda tid, max_quality=False: pytest.fail("streamed instead of the library")
    monkeypatch.setattr(providers, "build_providers", lambda *a, **k: {Service.QOBUZ: q})
    client_instance.engine.reset_providers(notify=False)

    tracks = _req(client_instance.url + "/api/search?q=nemo&kinds=tracks")["tracks"]
    row = next(t for t in tracks if t["service"] == "qobuz")
    assert row["library_id"].endswith("@" + server_instance.peer)
    r = _req(client_instance.url + "/api/resolve?service=qobuz&id=q-nemo")
    assert r["from_library"] and r["label"].endswith("· server")
    _, _, body = _req(client_instance.url + f"/stream/{r['token']}", raw=True)
    assert body == server_instance.files["nemo"].read_bytes()
