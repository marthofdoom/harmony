"""HTTP-layer hardening of the web server: request-body parsing for the game API
(Content-Length validation, the 1 MB cap) and HEAD for ``/stream/*``.

Raw sockets, because ``urllib`` will not send a negative or non-numeric
Content-Length (nor chunked bodies without help).
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from harmony import game_source as gs
from harmony.web import server as web_server


class _Engine:
    def __init__(self, tmp_path) -> None:
        self.key: str | None = None
        self.tmp = tmp_path
        self.audio = tmp_path / "song.bin"
        self.audio.write_bytes(bytes(range(256)) * 40)  # 10 240 bytes
        self.game_bodies: list[object] = []

    def check_key(self, provided):
        return not self.key or provided == self.key

    def stream_for(self, token):
        if token != "tok":
            return None
        return {"url": self.audio.as_uri(), "headers": {}, "mime": "audio/x-test"}

    def refresh_stream(self, _token):
        return None

    def game_request(self, method, parts, query, body, headers):
        self.game_bodies.append(body)
        return gs.GameResponse(200, {"ok": True, "method": method, "body": body})


@pytest.fixture
def srv(tmp_path, monkeypatch):
    eng = _Engine(tmp_path)
    monkeypatch.setattr(web_server, "_engine", eng)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_server.HarmonyHTTPRequestHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    yield SimpleNamespace(eng=eng, host=host, port=port)
    httpd.shutdown()
    httpd.server_close()


def raw(srv, request: bytes, *, read_body: bool = True):
    """Send ``request`` verbatim; returns ``(status, headers, body)``."""
    with socket.create_connection((srv.host, srv.port), timeout=10) as sock:
        sock.sendall(request)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {k.lower(): v for k, _, v in (ln.partition(": ") for ln in lines[1:])}
        body = rest
        if read_body:
            want = int(headers.get("content-length", "0"))
            while len(body) < want:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                body += chunk
        return status, headers, body


def post(srv, method="POST", path="/api/game/next", *, cl: str | None, body: bytes = b"", extra: str = ""):
    head = f"{method} {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{extra}"
    if cl is not None:
        head += f"Content-Length: {cl}\r\n"
    return raw(srv, head.encode() + b"\r\n" + body)


# -- request bodies ------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("cl", ["-1", "-5", "abc", "1.5", "0x10", "+3", "1 2"])
def test_game_bad_content_length_is_400_and_never_read_to_eof(srv, method, cl):
    path = "/api/game/next" if method == "POST" else "/api/game/profiles/x"
    status, _h, body = post(srv, method, path, cl=cl, body=b'{"a": 1}')   # would hang on read(-1)
    assert status == 400 and json.loads(body)["code"] == "bad_request"
    assert srv.eng.game_bodies == []


def test_game_chunked_body_is_411(srv):
    status, _h, body = post(srv, cl=None, extra="Transfer-Encoding: chunked\r\n", body=b"2\r\n{}\r\n0\r\n\r\n")
    assert status == 411 and json.loads(body)["code"] == "length_required"
    assert srv.eng.game_bodies == []


@pytest.mark.parametrize(("method", "path"), [("POST", "/api/game/next"), ("PUT", "/api/game/profiles/x"),
                                              ("DELETE", "/api/game/profiles/x")])
def test_game_body_over_1mb_is_413_for_post_put_delete(srv, method, path):
    status, _h, body = post(srv, method, path, cl=str(1_000_001))   # the body is never even sent
    assert status == 413 and json.loads(body)["code"] == "too_large"
    assert srv.eng.game_bodies == []


def test_game_body_at_the_cap_is_accepted(srv):
    payload = json.dumps({"pad": "x" * 999_000}).encode()
    status, _h, body = post(srv, cl=str(len(payload)), body=payload)
    assert status == 200 and len(srv.eng.game_bodies[0]["pad"]) == 999_000


def test_missing_content_length_means_an_empty_body_for_post_and_delete(srv):
    assert post(srv, cl=None)[0] == 200 and srv.eng.game_bodies == [{}]
    assert post(srv, "DELETE", "/api/game/profiles/x", cl=None)[0] == 200


def test_valid_body_still_parsed(srv):
    status, _h, body = post(srv, cl="21", body=b'{"profile": "skyrim"}\n')
    assert status == 200 and json.loads(body)["body"] == {"profile": "skyrim"}
    assert post(srv, cl="4", body=b"nope")[0] == 400                 # invalid JSON, as before


# -- HEAD /stream/* --------------------------------------------------------------------------------


def head(srv, path, extra=""):
    return raw(srv, f"HEAD {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{extra}\r\n".encode(),
               read_body=False)


def test_head_stream_answers_with_the_get_headers_and_no_body(srv):
    g_status, g_headers, g_body = raw(srv, b"GET /stream/tok HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert g_status == 200 and len(g_body) == 10240
    h_status, h_headers, h_body = head(srv, "/stream/tok")
    assert h_status == 200 and h_body == b""
    assert h_headers["content-length"] == g_headers["content-length"] == "10240"
    assert h_headers["content-type"] == g_headers["content-type"] == "audio/x-test"


def test_head_stream_honours_range(srv):
    status, headers, body = head(srv, "/stream/tok", "Range: bytes=100-199\r\n")
    assert status == 206 and body == b"" and headers["content-length"] == "100"
    assert headers["content-range"] == "bytes 100-199/10240"


def test_head_stream_unknown_token_is_404_with_no_body(srv):
    status, headers, body = head(srv, "/stream/nope")
    assert status == 404 and body == b"" and headers["content-length"] == "0"


def test_head_stream_needs_the_personal_key(srv):
    srv.eng.key = "secret"
    assert head(srv, "/stream/tok")[0] == 401
    assert head(srv, "/stream/tok", "X-Harmony-Key: secret\r\n")[0] == 200


@pytest.mark.parametrize("path", ["/", "/nonsense", "/api/search", "/healthz", "/stream", "/art/x"])
def test_head_elsewhere_is_still_404(srv, path):
    status, _h, body = head(srv, path)
    assert status == 404 and body == b""
