"""Offline tests for the headless web server (no GTK, no network).

Boots the real ``ThreadingHTTPServer`` on an ephemeral port and hits it over
loopback -- so this exercises the actual routing/handler, not a reimplementation.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from harmony import __version__
from harmony.web.server import HarmonyHTTPRequestHandler


@pytest.fixture
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), HarmonyHTTPRequestHandler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - loopback test server
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def _post(url: str, obj: dict):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - loopback test server
        return resp.status, resp.read()


def test_healthz_reports_ok_and_version(base_url: str) -> None:
    status, ctype, body = _get(base_url + "/healthz")
    assert status == 200
    assert "application/json" in ctype
    payload = json.loads(body)
    assert payload == {"status": "ok", "service": "harmony", "version": __version__}


def test_start_background_serves_and_stops() -> None:
    from harmony.web.server import start_background

    httpd = start_background("127.0.0.1", 0)  # ephemeral port
    try:
        host, port = httpd.server_address
        status, _, _ = _get(f"http://{host}:{port}/healthz")
        assert status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_root_serves_the_web_app_shell(base_url: str) -> None:
    status, ctype, body = _get(base_url + "/")
    assert status == 200
    assert "text/html" in ctype
    assert b"<title>Harmony</title>" in body


def test_unknown_api_route_is_404(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _get(base_url + "/api/bogus")
    assert exc.value.code == 404


def test_path_traversal_is_blocked(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _get(base_url + "/../../../../etc/passwd")
    assert exc.value.code == 404


# -- serve() defaults: public + no auth (reachable out of the box) ---------------


def test_serve_defaults_to_public_bind() -> None:
    from harmony.server import _build_parser

    args = _build_parser().parse_args([])
    assert args.address == "0.0.0.0"  # all interfaces, reachable by default
    assert args.port == 8080


def test_is_public_bind() -> None:
    from harmony.server import _is_public_bind

    assert _is_public_bind("0.0.0.0")
    assert _is_public_bind("::")
    assert not _is_public_bind("127.0.0.1")
    assert not _is_public_bind("100.64.0.1")  # a Tailscale address is not "all interfaces"


# -- API routing (fake engine, no providers/network) -----------------------------


class _FakeEngine:
    def __init__(self):
        self.calls = []
        self.key = None

    def check_key(self, provided):
        return not self.key or provided == self.key

    def instances(self):
        return {"instances": [{"name": "harmony-desk", "host": "192.168.1.5", "port": 8080, "version": "0.6.1"}]}

    def accounts(self):
        return {"accounts": [{"service": "qobuz", "authenticated": True, "account": "me"}]}

    def preferences(self):
        return {"personal_key": "k"}

    def set_preferences(self, personal_key=None):
        self.calls.append(("prefs", personal_key))
        return {"personal_key": personal_key}

    def set_qobuz_token(self, token):
        self.calls.append(("qobuz_token", token))
        return self.accounts()

    def set_ytmusic_browser(self, headers):
        self.calls.append(("yt_browser", headers))
        return self.accounts()

    def signout(self, service):
        if service not in ("qobuz", "ytmusic"):
            raise KeyError(service)
        self.calls.append(("signout", service))
        return self.accounts()

    def ytmusic_autodetect(self, browser=None):
        self.calls.append(("autodetect", browser))
        return self.accounts()

    def set_ytmusic_oauth_client(self, cid, cs):
        self.calls.append(("oauth_client", cid, cs))
        return {"ok": True}

    def ytmusic_oauth_start(self):
        self.calls.append(("oauth_start",))
        return {"poll_token": "pt", "user_code": "ABCD-1234", "verification_url": "https://g",
                "full_url": "https://g?x", "interval": 5, "expires_in": 300}

    def ytmusic_oauth_poll(self, token):
        self.calls.append(("oauth_poll", token))
        return {"status": "done", "accounts": []}

    def search(self, q, kinds, limit=25):
        return {"tracks": [{"id": "t1", "title": f"hit for {q}", "service": "qobuz",
                            "artist": "A", "album": "Alb", "duration_s": 200, "artwork_url": None}],
                "albums": [], "artists": [], "playlists": []}

    def playlists(self):
        return {"playlists": [{"id": "p1", "title": "Fav", "service": "qobuz",
                               "track_count": 3, "owner": "me", "artwork_url": None}]}

    def playlist_tracks(self, service, pid):
        if service != "qobuz":
            raise KeyError(service)
        return {"tracks": [{"id": "t1", "title": "Song", "service": "qobuz",
                            "artist": "A", "album": None, "duration_s": 100, "artwork_url": None}]}

    def resolve(self, service, track_id):
        return {"token": "tok123", "mime": "audio/flac", "label": "FLAC 24/96kHz"}

    def create_playlist(self, service, title):
        self.calls.append(("create", service, title))
        return {"playlist": {"id": "new", "title": title, "service": service}}

    def add_tracks(self, service, pid, ids):
        self.calls.append(("add", service, pid, tuple(ids)))
        return {"ok": True, "added": len(ids)}

    def remove_tracks(self, service, pid, ids):
        self.calls.append(("remove", service, pid, tuple(ids)))
        return {"ok": True, "removed": len(ids)}

    def rename_playlist(self, service, pid, title):
        self.calls.append(("rename", service, pid, title))
        return {"ok": True, "title": title}

    def delete_playlist(self, service, pid):
        self.calls.append(("delete", service, pid))
        return {"ok": True}

    def devices(self):
        return {"devices": [{"host": "192.168.1.9", "name": "Bedroom", "kind": "wiim"}]}

    def cast(self, host, service, track_id, meta=None):
        self.calls.append(("cast", host, service, track_id))
        return {"ok": True, "host": host}

    def device_control(self, host, action, level=None):
        self.calls.append(("control", host, action, level))
        return {"ok": True}

    def device_status(self, host):
        return {"state": "playing", "position_s": 5, "duration_s": 200, "volume": 40}

    def audio_sinks(self):
        return {"sinks": [{"name": "dac", "description": "USB DAC"}]}

    def audio_status(self):
        return {"receiving": False, "sending": False, "roc": True}

    def audio_receive(self, sink, latency_ms=150):
        self.calls.append(("audio_receive", sink, latency_ms))
        return {"ok": True, "sink": sink or "default", "transport": "roc", "latency_ms": latency_ms}

    def audio_send(self, to_host, latency_ms=150, transport=None):
        self.calls.append(("audio_send", to_host, latency_ms, transport))
        return {"ok": True, "to_host": to_host, "transport": transport or "roc"}

    def audio_stop(self):
        self.calls.append(("audio_stop",))
        return {"ok": True}

    def audio_route(self, direction, peer_host, peer_port, sink=None, latency_ms=150):
        self.calls.append(("audio_route", direction, peer_host, peer_port, sink, latency_ms))
        return {"ok": True, "direction": direction, "peer": peer_host}

    def sync_plan(self, source, target, direction):
        self.calls.append(("plan", source["service"], target["service"], direction))
        return {"token": "pl1", "adds": 3, "removes": 1, "unmatched": 0, "notes": []}

    def sync_apply(self, token):
        self.calls.append(("apply", token))
        return {"added": 3, "removed": 1, "failed": 0, "messages": []}

    def stream_for(self, token):
        return None  # exercised token path returns 404 in these tests


@pytest.fixture
def api_url(monkeypatch: pytest.MonkeyPatch):
    import harmony.web.server as srv

    monkeypatch.setattr(srv, "_engine", _FakeEngine())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.HarmonyHTTPRequestHandler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_api_search_returns_tracks(api_url: str) -> None:
    status, ctype, body = _get(api_url + "/api/search?q=hello")
    assert status == 200
    payload = json.loads(body)
    assert payload["tracks"][0]["title"] == "hit for hello"
    assert payload["tracks"][0]["service"] == "qobuz"


def test_api_search_without_query_is_400(api_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _get(api_url + "/api/search")
    assert exc.value.code == 400


def test_api_accounts_and_playlists(api_url: str) -> None:
    _, _, acc = _get(api_url + "/api/accounts")
    assert json.loads(acc)["accounts"][0]["account"] == "me"
    _, _, pls = _get(api_url + "/api/playlists")
    assert json.loads(pls)["playlists"][0]["title"] == "Fav"


def test_api_playlist_tracks_routing(api_url: str) -> None:
    _, _, body = _get(api_url + "/api/playlists/qobuz/p1/tracks")
    assert json.loads(body)["tracks"][0]["id"] == "t1"


def test_api_resolve_returns_a_token(api_url: str) -> None:
    _, _, body = _get(api_url + "/api/resolve?service=qobuz&id=t1")
    assert json.loads(body)["token"] == "tok123"


def test_stream_unknown_token_is_404(api_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _get(api_url + "/stream/nope")
    assert exc.value.code == 404


# -- mesh (LAN discovery) --------------------------------------------------------


def test_api_instances_lists_discovered_peers(api_url: str) -> None:
    _, _, body = _get(api_url + "/api/instances")
    assert json.loads(body)["instances"][0]["name"] == "harmony-desk"


def test_mesh_degrades_without_zeroconf(monkeypatch: pytest.MonkeyPatch) -> None:
    # With python-zeroconf absent, the mesh is a no-op: start() doesn't raise and
    # peers() is empty -- the app must run regardless.
    import builtins

    from harmony.mesh import Mesh

    real_import = builtins.__import__

    def no_zeroconf(name, *a, **k):
        if name == "zeroconf" or name.startswith("zeroconf."):
            raise ImportError("zeroconf disabled for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_zeroconf)
    mesh = Mesh("harmony-test", 8080)
    mesh.start()
    assert mesh.peers() == []
    mesh.stop()


# -- personal-key gate (mesh credential sharing) ---------------------------------


def test_personal_key_gate_blocks_and_allows(api_url: str) -> None:
    import harmony.web.server as srv

    srv._engine.key = "secret"
    # No key -> 401 on API and stream; static + healthz stay open.
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _get(api_url + "/api/accounts")
    assert exc.value.code == 401
    assert _get(api_url + "/healthz")[0] == 200
    assert _get(api_url + "/")[0] == 200

    # Correct key via header, and via ?key= query, both pass.
    req = urllib.request.Request(api_url + "/api/accounts", headers={"X-Harmony-Key": "secret"})
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
        assert r.status == 200
    assert _get(api_url + "/api/accounts?key=secret")[0] == 200

    # Wrong key -> 401.
    bad = urllib.request.Request(api_url + "/api/accounts", headers={"X-Harmony-Key": "nope"})
    with pytest.raises(urllib.error.HTTPError) as exc2:  # noqa: PT011
        urllib.request.urlopen(bad, timeout=5)  # noqa: S310
    assert exc2.value.code == 401


# -- preferences (personal key) --------------------------------------------------


def test_get_preferences(api_url: str) -> None:
    _, _, body = _get(api_url + "/api/preferences")
    assert json.loads(body)["personal_key"] == "k"


def test_post_preferences_sets_personal_key(api_url: str) -> None:
    import harmony.web.server as srv

    status, _ = _post(api_url + "/api/preferences", {"personal_key": "shared-secret"})
    assert status == 200
    assert ("prefs", "shared-secret") in srv._engine.calls


def test_settings_has_personal_key_field() -> None:
    from harmony.config import Settings

    assert Settings().personal_key == ""  # present, empty by default, on all versions


# -- playlist editing (POST) -----------------------------------------------------


def test_create_playlist(api_url: str) -> None:
    import harmony.web.server as srv

    status, body = _post(api_url + "/api/playlists", {"service": "qobuz", "title": "Road"})
    assert status == 200
    assert json.loads(body)["playlist"]["title"] == "Road"
    assert ("create", "qobuz", "Road") in srv._engine.calls


def test_create_playlist_missing_title_is_400(api_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _post(api_url + "/api/playlists", {"service": "qobuz"})
    assert exc.value.code == 400


def test_add_remove_rename_delete_routes(api_url: str) -> None:
    import harmony.web.server as srv

    _post(api_url + "/api/playlists/qobuz/p1/add", {"track_ids": ["t1", "t2"]})
    _post(api_url + "/api/playlists/qobuz/p1/remove", {"track_ids": ["t1"]})
    _post(api_url + "/api/playlists/qobuz/p1/rename", {"title": "New Name"})
    _post(api_url + "/api/playlists/qobuz/p1/delete", {})
    calls = srv._engine.calls
    assert ("add", "qobuz", "p1", ("t1", "t2")) in calls
    assert ("remove", "qobuz", "p1", ("t1",)) in calls
    assert ("rename", "qobuz", "p1", "New Name") in calls
    assert ("delete", "qobuz", "p1") in calls


# -- sync ------------------------------------------------------------------------


def test_sync_plan_and_apply(api_url: str) -> None:
    import harmony.web.server as srv

    _, body = _post(api_url + "/api/sync/plan", {
        "source": {"service": "ytmusic", "id": "s1"},
        "target": {"service": "qobuz", "id": "t1"}, "direction": "a_to_b"})
    plan = json.loads(body)
    assert plan["adds"] == 3 and plan["token"] == "pl1"
    _, rbody = _post(api_url + "/api/sync/apply", {"token": "pl1"})
    assert json.loads(rbody)["added"] == 3
    calls = srv._engine.calls
    assert ("plan", "ytmusic", "qobuz", "a_to_b") in calls
    assert ("apply", "pl1") in calls


def test_sync_plan_missing_fields_is_400(api_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _post(api_url + "/api/sync/plan", {"source": {"service": "ytmusic"}})
    assert exc.value.code == 400


# -- cast to device --------------------------------------------------------------


def test_devices_list_and_status(api_url: str) -> None:
    _, _, dev = _get(api_url + "/api/devices")
    assert json.loads(dev)["devices"][0]["name"] == "Bedroom"
    _, _, st = _get(api_url + "/api/devices/192.168.1.9/status")
    assert json.loads(st)["state"] == "playing"


def test_cast_play_and_transport(api_url: str) -> None:
    import harmony.web.server as srv

    _post(api_url + "/api/devices/192.168.1.9/play", {"service": "qobuz", "id": "t1"})
    _post(api_url + "/api/devices/192.168.1.9/pause", {})
    _post(api_url + "/api/devices/192.168.1.9/volume", {"level": 30})
    calls = srv._engine.calls
    assert ("cast", "192.168.1.9", "qobuz", "t1") in calls
    assert ("control", "192.168.1.9", "pause", None) in calls
    assert ("control", "192.168.1.9", "volume", 30) in calls


def test_cast_play_missing_id_is_400(api_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _post(api_url + "/api/devices/192.168.1.9/play", {"service": "qobuz"})
    assert exc.value.code == 400


# -- inter-instance audio routing ------------------------------------------------


def test_audio_sinks_and_status(api_url: str) -> None:
    _, _, sinks = _get(api_url + "/api/audio/sinks")
    assert json.loads(sinks)["sinks"][0]["name"] == "dac"
    _, _, st = _get(api_url + "/api/audio/status")
    assert json.loads(st)["roc"] is True


def test_audio_route_and_stop(api_url: str) -> None:
    import harmony.web.server as srv

    _post(api_url + "/api/audio/route", {"direction": "receive", "peer_host": "192.168.1.5",
                                         "peer_port": 8080, "latency_ms": 40})
    _post(api_url + "/api/audio/send", {"to_host": "192.168.1.5"})
    _post(api_url + "/api/audio/stop", {})
    calls = srv._engine.calls
    assert ("audio_route", "receive", "192.168.1.5", 8080, None, 40) in calls
    assert ("audio_send", "192.168.1.5", 150, None) in calls
    assert ("audio_stop",) in calls


def test_audio_route_bad_direction_is_400(api_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _post(api_url + "/api/audio/route", {"direction": "sideways", "peer_host": "h", "peer_port": 8080})
    assert exc.value.code == 400


def test_audio_send_missing_host_is_400(api_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _post(api_url + "/api/audio/send", {})
    assert exc.value.code == 400


def test_audio_send_forwards_rtp_transport(api_url: str) -> None:
    import harmony.web.server as srv

    # A phone asks the instance to send plain RTP so it can play without ROC.
    _post(api_url + "/api/audio/send", {"to_host": "192.168.1.7", "transport": "rtp"})
    assert ("audio_send", "192.168.1.7", 150, "rtp") in srv._engine.calls


# -- credential management (POST) ------------------------------------------------


def test_post_qobuz_token_seeds_and_returns_accounts(api_url: str) -> None:
    import harmony.web.server as srv

    status, body = _post(api_url + "/api/accounts/qobuz/token", {"token": "abc123"})
    assert status == 200
    assert json.loads(body)["accounts"][0]["service"] == "qobuz"
    assert ("qobuz_token", "abc123") in srv._engine.calls


def test_post_qobuz_token_missing_is_400(api_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _post(api_url + "/api/accounts/qobuz/token", {})
    assert exc.value.code == 400


def test_post_ytmusic_browser_seeds(api_url: str) -> None:
    import harmony.web.server as srv

    status, _ = _post(api_url + "/api/accounts/ytmusic/browser", {"headers": "Cookie: x"})
    assert status == 200
    assert ("yt_browser", "Cookie: x") in srv._engine.calls


def test_ytmusic_autodetect_route(api_url: str) -> None:
    import harmony.web.server as srv

    status, _ = _post(api_url + "/api/accounts/ytmusic/autodetect", {})
    assert status == 200
    assert ("autodetect", None) in srv._engine.calls


def test_build_headers_raw_shape() -> None:
    from harmony.providers.ytmusic_cookies import build_headers_raw

    assert build_headers_raw({"foo": "bar"}) is None  # no SAPISID -> not browser auth
    h = build_headers_raw({"SAPISID": "xyz", "HSID": "h"})
    assert "Authorization: SAPISIDHASH " in h
    assert "Cookie: SAPISID=xyz; HSID=h" in h
    assert "https://music.youtube.com" in h


def test_ytmusic_oauth_flow_routes(api_url: str) -> None:
    import harmony.web.server as srv

    _post(api_url + "/api/accounts/ytmusic/oauth/client", {"client_id": "cid", "client_secret": "cs"})
    _, sbody = _post(api_url + "/api/accounts/ytmusic/oauth/start", {})
    assert json.loads(sbody)["user_code"] == "ABCD-1234"
    _, pbody = _post(api_url + "/api/accounts/ytmusic/oauth/poll", {"poll_token": "pt"})
    assert json.loads(pbody)["status"] == "done"
    calls = srv._engine.calls
    assert ("oauth_client", "cid", "cs") in calls
    assert ("oauth_start",) in calls
    assert ("oauth_poll", "pt") in calls


def test_ytmusic_oauth_client_missing_is_400(api_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _post(api_url + "/api/accounts/ytmusic/oauth/client", {"client_id": "cid"})
    assert exc.value.code == 400


def test_ytmusic_oauth_poll_once_pending_then_success() -> None:
    """The shared device-flow poller: None while pending, raw token on success."""
    from harmony.providers import ytmusic_oauth

    class _Creds:
        def __init__(self):
            self.calls = 0

        def token_from_code(self, device_code):
            self.calls += 1
            return {"error": "authorization_pending"} if self.calls == 1 else {"access_token": "a"}

    creds = _Creds()
    assert ytmusic_oauth.poll_once(creds, "dc") is None       # pending
    assert ytmusic_oauth.poll_once(creds, "dc") == {"access_token": "a"}  # done


def test_post_signout_routes_service(api_url: str) -> None:
    import harmony.web.server as srv

    status, _ = _post(api_url + "/api/accounts/qobuz/signout", {})
    assert status == 200
    assert ("signout", "qobuz") in srv._engine.calls


def test_post_unknown_service_signout_is_404(api_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _post(api_url + "/api/accounts/spotify/signout", {})
    assert exc.value.code == 404


# -- serialization ---------------------------------------------------------------


def test_credential_store_env_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    from harmony.config import QOBUZ_TOKEN, CredentialStore

    monkeypatch.setenv("HARMONY_QOBUZ_USER_AUTH_TOKEN", "env-token")
    assert CredentialStore().get(QOBUZ_TOKEN) == "env-token"  # env wins over keyring/file


def test_track_to_dict_shape() -> None:
    from types import SimpleNamespace

    from harmony.models import Service
    from harmony.web.api import track_to_dict

    t = SimpleNamespace(id="x", title="T", service=Service.QOBUZ, artist_name="A",
                        album="Al", duration_s=123, artwork_url="u")
    d = track_to_dict(t)
    assert d == {"id": "x", "title": "T", "service": "qobuz", "artist": "A",
                 "album": "Al", "duration_s": 123, "artwork_url": "u"}
