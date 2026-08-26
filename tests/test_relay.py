"""Offline tests for harmony.playback.relay.

Both "ends" of the relay are localhost-only ``ThreadingHTTPServer``s bound to
port 0: a fake upstream (standing in for a provider CDN) and the
:class:`RelayServer` under test. Nothing here touches the real network.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from harmony.models import StreamSource
from harmony.playback.relay import RelayServer

PAYLOAD = os.urandom(100_000)

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class _FakeUpstreamHandler(BaseHTTPRequestHandler):
    """A minimal CDN double: serves PAYLOAD from /audio, with Range support.

    HTTP/1.0 (one request per connection) rather than 1.1/keep-alive: the
    relay's HEAD path intentionally doesn't drain the upstream body, and a
    keep-alive connection closed with an unread body logs a scary (but
    harmless) ConnectionResetError from the *next* accept() on this fake
    server -- not worth it for a test double.
    """

    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:
        if self.path != "/audio":
            self.send_error(404)
            return

        range_header = self.headers.get("Range")
        if range_header:
            match = _RANGE_RE.match(range_header)
            if match:
                start = int(match.group(1)) if match.group(1) else 0
                end = int(match.group(2)) if match.group(2) else len(PAYLOAD) - 1
                chunk = PAYLOAD[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
                return

        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - silence stderr
        pass


@pytest.fixture
def fake_upstream() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def relay() -> Iterator[RelayServer]:
    server = RelayServer(bind_host="127.0.0.1", port=0)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _register_audio_resolver(relay: RelayServer, fake_upstream: ThreadingHTTPServer) -> str:
    upstream_port = fake_upstream.server_address[1]

    def resolver() -> StreamSource:
        return StreamSource(url=f"http://127.0.0.1:{upstream_port}/audio", mime_type="audio/mpeg")

    return relay.register(resolver)


def test_get_relays_full_payload(relay: RelayServer, fake_upstream: ThreadingHTTPServer) -> None:
    token = _register_audio_resolver(relay, fake_upstream)

    resp = requests.get(f"http://127.0.0.1:{relay.port}/play/{token}", timeout=5)

    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "audio/mpeg"
    assert resp.content == PAYLOAD


def test_get_with_range_relays_partial_content(relay: RelayServer, fake_upstream: ThreadingHTTPServer) -> None:
    token = _register_audio_resolver(relay, fake_upstream)

    resp = requests.get(
        f"http://127.0.0.1:{relay.port}/play/{token}",
        headers={"Range": "bytes=10-19"},
        timeout=5,
    )

    assert resp.status_code == 206
    assert "Content-Range" in resp.headers
    assert resp.content == PAYLOAD[10:20]


def test_unknown_token_returns_404(relay: RelayServer) -> None:
    resp = requests.get(f"http://127.0.0.1:{relay.port}/play/does-not-exist", timeout=5)
    assert resp.status_code == 404


def test_head_returns_headers_without_body(relay: RelayServer, fake_upstream: ThreadingHTTPServer) -> None:
    token = _register_audio_resolver(relay, fake_upstream)

    resp = requests.head(f"http://127.0.0.1:{relay.port}/play/{token}", timeout=5)

    assert resp.status_code == 200
    assert resp.content == b""


def test_register_returns_unique_unguessable_tokens(relay: RelayServer, fake_upstream: ThreadingHTTPServer) -> None:
    token_a = _register_audio_resolver(relay, fake_upstream)
    token_b = _register_audio_resolver(relay, fake_upstream)
    assert token_a != token_b
    assert len(token_a) > 16


def test_url_for_returns_a_reachable_play_url(relay: RelayServer, fake_upstream: ThreadingHTTPServer) -> None:
    token = _register_audio_resolver(relay, fake_upstream)
    url = relay.url_for(token, "127.0.0.1")
    assert url.endswith(f":{relay.port}/play/{token}")
    resp = requests.get(url, timeout=5)
    assert resp.status_code == 200
    assert resp.content == PAYLOAD


def test_start_is_idempotent_and_port_is_stable() -> None:
    server = RelayServer(bind_host="127.0.0.1", port=0)
    server.start()
    try:
        first_port = server.port
        server.start()
        assert server.port == first_port
    finally:
        server.stop()
        server.stop()  # idempotent


def test_port_before_start_raises() -> None:
    server = RelayServer(bind_host="127.0.0.1", port=0)
    with pytest.raises(RuntimeError):
        _ = server.port


# --------------------------------------------------------------------------
# ICY stream metadata (so a renderer shows title/artist for a bare URL)
# --------------------------------------------------------------------------


def _deinterleave_icy(body: bytes, metaint: int) -> tuple[bytes, list[str]]:
    """Split an ICY body into (audio, [StreamTitle strings]) given the metaint."""
    audio = bytearray()
    metas: list[str] = []
    i = 0
    while i < len(body):
        audio += body[i : i + metaint]
        i += metaint
        if i >= len(body):
            break
        length = body[i] * 16
        i += 1
        meta = body[i : i + length]
        i += length
        text = meta.rstrip(b"\x00").decode("utf-8", "replace")
        if text:
            metas.append(text)
    return bytes(audio), metas


def _register_with_meta(relay: RelayServer, fake_upstream: ThreadingHTTPServer, title: str, artist: str) -> str:
    port = fake_upstream.server_address[1]

    def resolver() -> StreamSource:
        return StreamSource(url=f"http://127.0.0.1:{port}/audio", mime_type="audio/mpeg")

    return relay.register(resolver, title=title, artist=artist)


def test_icy_metadata_interleaved_when_requested(relay: RelayServer, fake_upstream: ThreadingHTTPServer) -> None:
    token = _register_with_meta(relay, fake_upstream, title="Roygbiv", artist="Boards of Canada")

    resp = requests.get(
        f"http://127.0.0.1:{relay.port}/play/{token}",
        headers={"Icy-MetaData": "1"},
        timeout=5,
    )

    assert resp.status_code == 200
    metaint = int(resp.headers["icy-metaint"])
    assert resp.headers.get("icy-name") == "Boards of Canada - Roygbiv"
    audio, metas = _deinterleave_icy(resp.content, metaint)
    assert audio == PAYLOAD  # de-interleaving recovers the exact upstream bytes
    assert metas and all(m == "StreamTitle='Boards of Canada - Roygbiv';" for m in metas)


def test_no_icy_without_metadata_even_if_requested(relay: RelayServer, fake_upstream: ThreadingHTTPServer) -> None:
    token = _register_audio_resolver(relay, fake_upstream)  # registered without title/artist

    resp = requests.get(
        f"http://127.0.0.1:{relay.port}/play/{token}",
        headers={"Icy-MetaData": "1"},
        timeout=5,
    )

    assert resp.status_code == 200
    assert "icy-metaint" not in resp.headers
    assert resp.content == PAYLOAD  # plain passthrough, no interleaving


def test_icy_metadata_block_is_padded_and_length_prefixed() -> None:
    from harmony.playback.relay import _icy_metadata_block

    block = _icy_metadata_block("Title", "Artist")
    length = block[0] * 16
    assert len(block) == 1 + length  # length byte counts 16-byte units
    assert block[1 : 1 + length].rstrip(b"\x00") == b"StreamTitle='Artist - Title';"
