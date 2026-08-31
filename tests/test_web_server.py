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

    def accounts(self):
        return {"accounts": [{"service": "qobuz", "authenticated": True, "account": "me"}]}

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
