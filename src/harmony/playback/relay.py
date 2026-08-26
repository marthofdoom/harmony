"""Local HTTP audio relay: forward a provider stream's bytes to a LAN renderer.

A WiiM/LinkPlay device (``PlaybackDevice.play_url``) can only play from a URL
it can fetch itself. For YouTube in particular the device cannot fetch the
CDN directly -- ``googlevideo`` URLs are IP/fingerprint-locked to the client
that resolved them -- so Harmony must fetch the stream itself and forward the
bytes. See ``docs/design/playback.md`` ("Play-to-device: the passive-relay
design") for the full rationale.

:class:`RelayServer` is a small ``http.server.ThreadingHTTPServer`` wrapper.
Callers ``register`` a zero-arg *resolver* -- ``Callable[[], StreamSource]``
-- that produces a fresh, unexpired stream on demand (called once per HTTP
request, never cached here, since provider URLs are time-limited) and get
back an opaque token. ``url_for`` turns that token into an absolute URL a
given device can reach, so ``device.play_url(relay.url_for(token, device.host))``
is the whole integration surface. The relay itself is passive: no decode, no
transcode, just a byte-for-byte pipe with ``Range`` passthrough for seeking.
"""

from __future__ import annotations

import logging
import secrets
import socket
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import requests

from ..models import StreamSource

log = logging.getLogger(__name__)

# Zero-arg callable returning a *fresh* StreamSource; invoked per fetch so the
# URL handed to the device is never stale (provider stream URLs are
# time-limited and often signed/IP-bound).
Resolver = Callable[[], StreamSource]

# Cap on how many outstanding tokens we remember, so a long-running instance
# that keeps registering new tracks doesn't grow the registry without bound.
# Old tokens simply 404 once evicted -- fine, since a device only ever plays
# the most recently pushed track(s).
_MAX_TOKENS = 64

# Connect / read timeouts for the upstream fetch: generous read timeout since
# this is a long-lived streaming body, not a quick API call.
_UPSTREAM_TIMEOUT_S = (10, 60)

_CHUNK_SIZE = 64 * 1024

# Response headers copied verbatim from the upstream reply when present, so
# the device sees the same seek/length semantics the provider CDN offers.
_PASSTHROUGH_HEADERS = ("Accept-Ranges", "Content-Length", "Content-Range")

# Bytes of audio between ICY metadata blocks. 16 KiB is the common radio-stream
# default and what LinkPlay/WiiM firmware expects when it requests metadata.
_ICY_METAINT = 16384


@dataclass(frozen=True)
class _Entry:
    """A registered playable: the per-fetch resolver plus static now-playing text."""

    resolver: Resolver
    title: str | None = None
    artist: str | None = None


def _icy_stream_title(title: str | None, artist: str | None) -> str:
    return " - ".join(part for part in (artist, title) if part)


def _icy_metadata_block(title: str | None, artist: str | None) -> bytes:
    """Build an ICY in-stream metadata block: a length byte + a padded StreamTitle.

    ICY has no escaping, so the quote/semicolon delimiters are stripped rather
    than escaped. The payload is zero-padded to a 16-byte multiple and prefixed
    with a single byte counting those 16-byte units, per the SHOUTcast
    convention every LinkPlay renderer implements.
    """
    text = _icy_stream_title(title, artist).replace("'", "").replace(";", "")
    payload = f"StreamTitle='{text}';".encode("utf-8", "replace")
    payload += b"\x00" * (-len(payload) % 16)
    return bytes([len(payload) // 16]) + payload


class RelayServer:
    """Serves ``GET/HEAD /play/<token>`` by resolving and re-streaming a track.

    One instance is meant to live for the app's lifetime. Register a resolver
    per track (or per playback attempt) with :meth:`register`, then build a
    device-reachable URL with :meth:`url_for` and hand that to
    ``PlaybackDevice.play_url``.
    """

    def __init__(self, *, bind_host: str = "0.0.0.0", port: int = 0) -> None:
        self._bind_host = bind_host
        self._requested_port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def start(self) -> None:
        """Bind and start serving in a daemon thread. Safe to call more than once."""
        if self._server is not None:
            return
        server = ThreadingHTTPServer((self._bind_host, self._requested_port), _Handler)
        server.relay = self  # back-reference so the handler can reach the registry
        self._server = server
        # A short poll_interval keeps shutdown() (used by stop(), and by tests
        # tearing down many short-lived servers) snappy -- serve_forever only
        # notices the shutdown flag once per interval, so the default 0.5s
        # would otherwise make every stop() pay up to half a second.
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, name="harmony-relay", daemon=True
        )
        self._thread = thread
        thread.start()
        log.info("relay server listening on %s:%d", self._bind_host, self.port)

    def stop(self) -> None:
        """Stop serving and release the socket. Safe to call more than once, or before start()."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None

    @property
    def port(self) -> int:
        """The actually-bound port (resolved even when constructed with ``port=0``)."""
        if self._server is None:
            raise RuntimeError("RelayServer has not been started")
        return self._server.server_address[1]

    def register(self, resolver: Resolver, *, title: str | None = None, artist: str | None = None) -> str:
        """Register a resolver (with optional now-playing text) and return a token.

        ``title``/``artist`` are handed to a device via ICY stream metadata when
        it asks for it, so the renderer can show what's playing for a bare URL.
        """
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._entries[token] = _Entry(resolver=resolver, title=title, artist=artist)
            while len(self._entries) > _MAX_TOKENS:
                self._entries.popitem(last=False)  # evict oldest
        return token

    def resolver_for(self, token: str) -> Resolver | None:
        """Thread-safe resolver lookup (for callers that only need the resolver)."""
        entry = self.entry_for(token)
        return entry.resolver if entry is not None else None

    def entry_for(self, token: str) -> _Entry | None:
        """Thread-safe lookup of the full registration used by the request handler."""
        with self._lock:
            return self._entries.get(token)

    def url_for(self, token: str, device_host: str) -> str:
        """Build the URL a device at ``device_host`` should use to fetch ``token``.

        Binding to ``0.0.0.0`` means the server itself has no single "local
        IP" to hand out, so we pick the address that actually routes to the
        device: open a UDP socket "connected" to it (no packets sent for UDP
        connect) and read back the outbound interface address from
        ``getsockname()``. Falls back to the host's resolved hostname, then
        to loopback, if that fails (e.g. no route / sandboxed network).
        """
        ip = self._local_ip_for(device_host)
        return f"http://{ip}:{self.port}/play/{token}"

    @staticmethod
    def _local_ip_for(device_host: str) -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((device_host, 80))
                return sock.getsockname()[0]
        except OSError:
            pass
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


class _Handler(BaseHTTPRequestHandler):
    """Handles ``GET``/``HEAD /play/<token>`` by resolving and re-streaming a track.

    ``self.server`` is the :class:`ThreadingHTTPServer` created in
    :meth:`RelayServer.start`, with ``.relay`` set to the owning
    :class:`RelayServer` -- see ``ThreadingHTTPServer.relay`` there.
    """

    protocol_version = "HTTP/1.1"

    def do_HEAD(self) -> None:
        self._handle(send_body=False)

    def do_GET(self) -> None:
        self._handle(send_body=True)

    def _handle(self, *, send_body: bool) -> None:
        # Logged at INFO so a device's request headers can be inspected by
        # running the app from a terminal -- key for knowing whether a renderer
        # asks for ICY metadata (Icy-MetaData: 1) or treats the URL as a file.
        log.info(
            "relay %s %s from %s | headers=%s",
            self.command,
            self.path,
            self.client_address[0],
            dict(self.headers),
        )
        token = self._token_from_path()
        if token is None:
            self.send_error(404, "Not Found")
            return

        relay: RelayServer = self.server.relay  # type: ignore[attr-defined]
        entry = relay.entry_for(token)
        if entry is None:
            self.send_error(404, "Unknown or expired token")
            return

        try:
            source = entry.resolver()
        except Exception:
            log.exception("relay: resolver failed for token %s", token)
            self.send_error(502, "Failed to resolve stream")
            return

        # Serve ICY in-stream metadata only when the device asks for it and we
        # have something to show. In that mode the body is streamed from the
        # start (no Range), so metadata blocks land at fixed offsets.
        wants_icy = self.headers.get("Icy-MetaData") == "1" and bool(entry.title or entry.artist)
        request_headers = dict(source.headers)
        if not wants_icy:
            range_header = self.headers.get("Range")
            if range_header:
                request_headers["Range"] = range_header

        try:
            upstream = requests.get(
                source.url,
                headers=request_headers,
                stream=True,
                timeout=_UPSTREAM_TIMEOUT_S,
                allow_redirects=True,
            )
        except requests.RequestException:
            log.exception("relay: upstream fetch failed for token %s", token)
            self.send_error(502, "Failed to fetch upstream stream")
            return

        with upstream:
            try:
                if wants_icy:
                    self._reply_icy(upstream, source, entry, send_body=send_body)
                else:
                    self._reply(upstream, source, send_body=send_body)
            except (BrokenPipeError, ConnectionResetError):
                # Device seeked or stopped mid-stream; nothing to do.
                pass

    def _reply_icy(
        self, upstream: requests.Response, source: StreamSource, entry: _Entry, *, send_body: bool
    ) -> None:
        """Stream the body with ICY metadata so the renderer shows title/artist.

        A metadata block carrying ``StreamTitle`` is inserted after every
        ``_ICY_METAINT`` bytes of audio. The stream is length-less and
        non-seekable, so it's close-delimited.
        """
        self.send_response(200)
        self.send_header(
            "Content-Type", source.mime_type or upstream.headers.get("Content-Type", "audio/mpeg")
        )
        self.send_header("icy-metaint", str(_ICY_METAINT))
        name = _icy_stream_title(entry.title, entry.artist)
        if name:
            self.send_header("icy-name", name)
        self.close_connection = True
        self.end_headers()
        if not send_body:
            return

        meta_block = _icy_metadata_block(entry.title, entry.artist)
        remaining = _ICY_METAINT
        for chunk in upstream.iter_content(_CHUNK_SIZE):
            if not chunk:
                continue
            view = memoryview(chunk)
            while len(view) >= remaining:
                self.wfile.write(view[:remaining])
                self.wfile.write(meta_block)
                view = view[remaining:]
                remaining = _ICY_METAINT
            if len(view):
                self.wfile.write(bytes(view))
                remaining -= len(view)

    def _reply(self, upstream: requests.Response, source: StreamSource, *, send_body: bool) -> None:
        status = upstream.status_code if upstream.status_code in (200, 206) else 200
        self.send_response(status)
        self.send_header("Content-Type", source.mime_type or upstream.headers.get("Content-Type", "application/octet-stream"))
        has_length = False
        for name in _PASSTHROUGH_HEADERS:
            value = upstream.headers.get(name)
            if value is not None:
                self.send_header(name, value)
                if name == "Content-Length":
                    has_length = True
        # protocol_version is HTTP/1.1, so the connection is keep-alive by
        # default and the client frames the body by Content-Length. When the
        # upstream streams without one (e.g. a chunked source), the client
        # would otherwise wait forever for a body-end it can't detect -- so
        # fall back to close-delimited framing for this response.
        if not has_length:
            self.close_connection = True
        self.end_headers()

        if not send_body:
            return

        for chunk in upstream.iter_content(_CHUNK_SIZE):
            if chunk:
                self.wfile.write(chunk)

    def _token_from_path(self) -> str | None:
        path = urlsplit(self.path).path
        prefix = "/play/"
        if not path.startswith(prefix):
            return None
        token = path[len(prefix) :]
        return token or None

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        log.debug("%s - %s", self.address_string(), format % args)


__all__ = ["RelayServer", "Resolver"]
