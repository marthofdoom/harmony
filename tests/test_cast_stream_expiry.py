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
        self.lie_after_truncate = None  # (start_shift, total_shift) for replies after a cut
        self.lied = 0
        self._cut = False
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
                    shown_start, total = start, len(PAYLOAD)
                    if cdn._cut and cdn.lie_after_truncate:
                        cdn.lied += 1
                        shown_start += cdn.lie_after_truncate[0]
                        total += cdn.lie_after_truncate[1]
                    self.send_header("Content-Range", f"bytes {shown_start}-{end}/{total}")
                self.send_header("Content-Length", str(len(part)))
                self.end_headers()
                if cdn.truncate_next > 0 and len(part) > 1024:
                    cdn.truncate_next -= 1
                    cdn._cut = True
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


# -- continuity of a resumed body ------------------------------------------------


@pytest.mark.parametrize("lie", [(7, 0), (0, 4096)], ids=["wrong-offset", "different-file"])
def test_a_resume_that_is_not_a_continuation_is_never_spliced(relay, lie) -> None:
    """After a cut, a re-fetch whose Content-Range doesn't start where the dead body
    stopped, or describes a resource of another length (a re-resolve to another
    quality tier), must not be stitched on: the request fails and the device retries."""
    cdn = _Cdn(ttl=60)
    try:
        cdn.truncate_next = 1
        cdn.lie_after_truncate = lie
        src = cdn.mint()
        token = relay.register(lambda: src, allow_icy=False, refresh=cdn.mint)
        got, problem = _fetch_like_a_chromecast(relay.url_for(token, "127.0.0.1"), pause=0)
        assert got == 0 and problem is not None and problem.startswith("chunk 0")
        assert cdn.lied >= 1  # the lying reply was fetched ... and refused
    finally:
        cdn.close()


def test_resume_tries_the_entrys_own_resolver_before_the_captured_source(monkeypatch) -> None:
    from harmony.playback import relay as relaymod

    stale = StreamSource(url="http://cdn/stale", mime_type="audio/flac")
    fresh = StreamSource(url="http://cdn/fresh", mime_type="audio/flac")
    entry = relaymod._Entry(resolver=lambda: fresh, refresh=None)
    seen: list[str] = []

    class Up:
        status_code = 206
        headers = {"Content-Range": "bytes 5-9/10"}

        def close(self) -> None:
            pass

    def fake_fetch(source, rng):
        seen.append(source.url)
        return Up()

    h = object.__new__(relaymod._Handler)
    monkeypatch.setattr(relaymod._Handler, "_fetch", staticmethod(fake_fetch))
    assert h._reopen(entry, stale, "bytes=5-9", "t", 10) is not None
    assert seen == ["http://cdn/fresh"]


class _Up:
    def __init__(self, chunks, headers, status=200) -> None:
        self._chunks, self.headers, self.status_code = chunks, headers, status
        self.closed = False

    def iter_content(self, _n):
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


def _handler():
    import io

    from harmony.playback import relay as relaymod

    h = object.__new__(relaymod._Handler)
    h.wfile = io.BytesIO()
    h.close_connection = False
    h.send_response = lambda *_a: None
    h.send_header = lambda *_a: None
    h.end_headers = lambda: None
    return h


def test_the_body_never_exceeds_the_declared_content_length() -> None:
    h = _handler()
    up = _Up([b"x" * 8, b"y" * 8], {"Content-Length": "10"})
    h._reply(up, StreamSource(url="http://c/x", mime_type="audio/flac"), send_body=True)
    assert h.wfile.getvalue() == b"x" * 8 + b"y" * 2


def test_resumed_responses_are_closed() -> None:
    h = _handler()
    first = _Up([b"a" * 4], {"Content-Length": "12", "Content-Range": "bytes 0-11/12"}, 206)
    second = _Up([b"b" * 4], {"Content-Range": "bytes 4-11/12"}, 206)  # cut short again
    third = _Up([b"c" * 4], {"Content-Range": "bytes 8-11/12"}, 206)
    nxts = [second, third]
    asked: list[tuple[str, int | None]] = []

    def reopen(rng, total):
        asked.append((rng, total))
        return nxts.pop(0)

    h._reply(first, StreamSource(url="http://c/x", mime_type="audio/flac"), send_body=True,
             reopen=reopen)
    assert h.wfile.getvalue() == b"a" * 4 + b"b" * 4 + b"c" * 4
    assert asked == [("bytes=4-11", 12), ("bytes=8-11", 12)]
    assert first.closed and second.closed and third.closed


def test_a_resume_that_raises_still_closes_what_it_opened() -> None:
    h = _handler()
    first = _Up([b"a" * 4], {"Content-Length": "12"}, 200)
    second = _Up([b"b" * 4], {}, 206)
    writes: list[bytes] = []

    class Wfile:
        def write(self, data: bytes) -> None:
            if len(writes) >= 2:  # the device went away during the resumed body
                raise BrokenPipeError
            writes.append(bytes(data))

    h.wfile = Wfile()
    with pytest.raises(BrokenPipeError):
        h._reply(first, StreamSource(url="http://c/x", mime_type="audio/flac"), send_body=True,
                 reopen=lambda rng, total: second)
    assert writes == [b"a" * 4, b"b" * 4] and second.closed


# -- when a signed URL is considered stale ------------------------------------------


def test_url_expiry_is_read_from_the_url() -> None:
    soon = int(time.time()) + 600
    assert castmod.url_expiry_epoch(f"https://rr1.googlevideo.com/videoplayback?id=1&expire={soon}&x=2") == soon
    assert castmod.url_expiry_epoch(f"https://streaming.qobuz.example/file?uid=1&etsp={soon}") == soon
    assert castmod.url_expiry_epoch(f"https://h.googlevideo.com/api/manifest/expire/{soon}/id/1") == soon
    assert castmod.url_expiry_epoch("https://cdn/x?expire=notanumber") is None
    assert castmod.url_expiry_epoch("https://cdn/x?expire=3600") is None  # a ttl, not an epoch
    assert castmod.url_expiry_epoch("https://cdn/plain") is None


def test_a_url_with_a_far_expiry_is_not_re_extracted_every_150_seconds(monkeypatch) -> None:
    far = int(time.time()) + 6 * 3600
    src = StreamSource(url=f"https://rr1.googlevideo.com/videoplayback?expire={far}", mime_type="audio/mp4")
    calls: list[int] = []
    fresh = castmod._FreshSource(lambda: calls.append(1) or src, src)
    fresh._at -= 10_000  # the age heuristic alone would call this stale many times over
    assert fresh.get() is src and calls == []


def test_a_url_close_to_its_expiry_is_renewed_before_it_lapses() -> None:
    exp = int(time.time()) + 3600
    old = StreamSource(url=f"https://cdn/x?expire={exp}", mime_type="audio/mp4")
    new = StreamSource(url=f"https://cdn/x?expire={exp + 3600}", mime_type="audio/mp4")
    fresh = castmod._FreshSource(lambda: new, old)
    assert fresh.get() is old  # plenty of life left
    fresh._at -= 3560  # 40 s from the end: inside the refresh margin
    fresh._expiry -= 3560
    assert fresh.get() is new


def test_failed_re_resolves_back_off_instead_of_retrying_every_request(monkeypatch) -> None:
    src = StreamSource(url="http://cdn/x", mime_type="audio/flac")
    calls: list[int] = []

    def boom() -> StreamSource:
        calls.append(1)
        raise RuntimeError("yt-dlp down")

    fresh = castmod._FreshSource(boom, src)
    monkeypatch.setattr(castmod, "SOURCE_STALE_S", 0.0)
    time.sleep(0.01)
    for _ in range(5):  # five chunk requests while the provider is failing
        assert fresh.get() is src
    assert len(calls) == 1
    # ... but the CDN's own 403 still forces an attempt, and a success clears the back-off
    ok = StreamSource(url="http://cdn/y", mime_type="audio/flac")
    fresh._resolve = lambda: ok
    assert fresh.refresh() is ok
    assert fresh._retry_at == 0.0
