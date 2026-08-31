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


def test_unknown_api_route_is_501_not_implemented(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _get(base_url + "/api/search")
    assert exc.value.code == 501


def test_path_traversal_is_blocked(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: PT011
        _get(base_url + "/../../../../etc/passwd")
    assert exc.value.code == 404
