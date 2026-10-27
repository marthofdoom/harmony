"""A cast device pulls a track from the relay in Range chunks over the whole song.

The streaming service's signed URL can expire (or the CDN can drop a long
connection) part-way through; the relay must keep the device fed instead of
ending the track early. A local 'CDN' that expires a token after a TTL stands in
for Qobuz/googlevideo; ``_fetch_like_a_chromecast`` stands in for the receiver.
Nothing touches the network.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import requests

from harmony.models import StreamSource
from harmony.playback.relay import RelayServer
from harmony.web import cast as castmod
from harmony.web.cast import CastController

CHUNK = 32 * 1024
CHUNKS = 8
PAYLOAD = os.urandom(CHUNK * CHUNKS)
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


class _Cdn:
    """Signed-URL CDN double: ``/track?t=<token>`` works for ``ttl`` s after minting."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self.issued: dict[str, float] = {}
        self.minted = 0
        self.requests = 0
        self.truncate_next = 0  # cut the next N range replies short (connection dropped)
        cdn = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self) -> None:
                cdn.requests += 1
                token = self.path.partition("t=")[2]
                born = cdn.issued.get(token)
                if born is None or time.monotonic() - born > cdn.ttl:
                    body = b"<Error>expired</Error>"
                    self.send_response(403)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                start, end = 0, len(PAYLOAD) - 1
                m = _RANGE.match(self.headers.get("Range", ""))
                if m:
                    start = int(m.group(1) or 0)
                    end = int(m.group(2)) if m.group(2) else end
                part = PAYLOAD[start:end + 1]
                self.send_response(206 if m else 200)
                if m:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
                self.send_header("Content-Length", str(len(part)))
                self.end_headers()
                if cdn.truncate_next > 0 and len(part) > 1024:
                    cdn.truncate_next -= 1
                    self.wfile.write(part[: len(part) // 2])  # then the socket closes
                    return
                self.wfile.write(part)

            def log_message(self, *a: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()

    def mint(self) -> StreamSource:
        self.minted += 1
        token = f"tok{self.minted}"
        self.issued[token] = time.monotonic()
        return StreamSource(url=f"http://127.0.0.1:{self.server.server_address[1]}/track?t={token}",
                            mime_type="audio/flac")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _fetch_like_a_chromecast(url: str, pause: float) -> tuple[int, str | None]:
    """Fetch the track in Range chunks, one every ``pause`` s; stop at the first
    reply that isn't the audio asked for (a receiver ends the track there).
    Returns (bytes received intact, problem)."""
    got = 0
    for i in range(CHUNKS):
        lo, hi = i * CHUNK, (i + 1) * CHUNK - 1
        try:
            r = requests.get(url, headers={"Range": f"bytes={lo}-{hi}"}, timeout=5)
        except requests.RequestException as exc:
            return got, f"chunk {i}: {exc.__class__.__name__}"
        if r.status_code != 206 or r.content != PAYLOAD[lo:hi + 1]:
            return got, f"chunk {i}: HTTP {r.status_code}, {len(r.content)} bytes"
        got += len(r.content)
        time.sleep(pause)
    return got, None


class _FakeCastDevice:
    def __init__(self) -> None:
        self.url: str | None = None

    def play_url(self, url: str, **_kw: Any) -> None:
        self.url = url


@pytest.fixture
def relay() -> Iterator[RelayServer]:
    server = RelayServer(bind_host="127.0.0.1", port=0)
    server.start()
    yield server
    server.stop()


def _controller(relay: RelayServer, cdn: _Cdn, monkeypatch: pytest.MonkeyPatch) -> tuple[CastController, _FakeCastDevice]:
    ctl = CastController(lambda _svc, _tid: cdn.mint())
    ctl._relay = relay
    dev = _FakeCastDevice()
    monkeypatch.setattr(ctl, "_device", lambda *a, **k: dev)
    return ctl, dev


def test_stale_stream_is_re_resolved_before_a_late_chunk(relay, monkeypatch) -> None:
    """Track far longer than the URL's life: chunks keep arriving because the
    resolver re-resolves once the URL is old, before the CDN refuses it."""
    cdn = _Cdn(ttl=0.7)
    try:
        monkeypatch.setattr(castmod, "SOURCE_STALE_S", 0.3)
        ctl, dev = _controller(relay, cdn, monkeypatch)
        ctl.cast("127.0.0.1", "qobuz", "1", kind="cast")
        got, problem = _fetch_like_a_chromecast(dev.url, pause=0.25)  # ~2 s > ttl
        assert problem is None and got == len(PAYLOAD)
        assert cdn.minted >= 3
    finally:
        cdn.close()


def test_expired_url_is_retried_once_with_a_fresh_resolve(relay, monkeypatch) -> None:
    """Even when the age heuristic doesn't fire, an upstream 403 mid-track
    triggers one re-resolve and the device never sees the failure."""
    cdn = _Cdn(ttl=0.9)
    try:
        monkeypatch.setattr(castmod, "SOURCE_STALE_S", 10_000.0)
        ctl, dev = _controller(relay, cdn, monkeypatch)
        ctl.cast("127.0.0.1", "qobuz", "1", kind="cast")
        got, problem = _fetch_like_a_chromecast(dev.url, pause=0.2)  # ~1.6 s > ttl
        assert problem is None and got == len(PAYLOAD)
        assert cdn.minted == 2  # the original + exactly one refresh
    finally:
        cdn.close()


def test_an_upstream_error_is_never_relayed_as_audio(relay) -> None:
    """Without a refresh, a 403 from the CDN must not be passed on as a 200 whose
    body is the error page (the device would decode it as audio)."""
    cdn = _Cdn(ttl=0.0)
    try:
        src = cdn.mint()
        time.sleep(0.01)
        token = relay.register(lambda: src, allow_icy=False)
        r = requests.get(relay.url_for(token, "127.0.0.1"), timeout=5)
        assert r.status_code == 502 and b"expired" not in r.content
    finally:
        cdn.close()


def test_a_still_refused_url_after_refresh_is_an_error_not_audio(relay) -> None:
    cdn = _Cdn(ttl=0.0)
    try:
        time.sleep(0.01)
        token = relay.register(cdn.mint, allow_icy=False, refresh=cdn.mint)
        r = requests.get(relay.url_for(token, "127.0.0.1"), timeout=5)
        assert r.status_code == 502
        assert cdn.minted == 2  # retried once, not in a loop
    finally:
        cdn.close()


def test_library_files_are_never_re_resolved(monkeypatch) -> None:
    calls: list[int] = []
    src = StreamSource(url="file:///music/a.flac", mime_type="audio/flac")
    fresh = castmod._FreshSource(lambda: calls.append(1) or src, src)
    monkeypatch.setattr(castmod, "SOURCE_STALE_S", 0.0)
    time.sleep(0.01)
    assert fresh.get() is src and calls == []


def test_failed_re_resolve_keeps_serving_the_old_url(monkeypatch) -> None:
    src = StreamSource(url="http://cdn/x", mime_type="audio/flac")

    def boom() -> StreamSource:
        raise RuntimeError("api down")

    fresh = castmod._FreshSource(boom, src)
    monkeypatch.setattr(castmod, "SOURCE_STALE_S", 0.0)
    time.sleep(0.01)
    assert fresh.get() is src
    with pytest.raises(RuntimeError):
        fresh.refresh()


def test_body_cut_short_by_the_cdn_is_resumed_by_byte_offset(relay) -> None:
    """A CDN dropping the connection mid-chunk must not hand the device a short
    body (it would take that as the end of the media)."""
    cdn = _Cdn(ttl=60)
    try:
        cdn.truncate_next = 3
        src = cdn.mint()
        token = relay.register(lambda: src, allow_icy=False, refresh=cdn.mint)
        got, problem = _fetch_like_a_chromecast(relay.url_for(token, "127.0.0.1"), pause=0)
        assert problem is None and got == len(PAYLOAD)
    finally:
        cdn.close()
