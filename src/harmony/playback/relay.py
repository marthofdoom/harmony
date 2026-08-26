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
        self._resolvers: OrderedDict[str, Resolver] = OrderedDict()

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

    def register(self, resolver: Resolver) -> str:
        """Register a resolver and return a fresh, unguessable token for it."""
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._resolvers[token] = resolver
            while len(self._resolvers) > _MAX_TOKENS:
                self._resolvers.popitem(last=False)  # evict oldest
        return token

    def resolver_for(self, token: str) -> Resolver | None:
        """Thread-safe lookup used by the request handler."""
        with self._lock:
            return self._resolvers.get(token)

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
        token = self._token_from_path()
        if token is None:
            self.send_error(404, "Not Found")
            return

        relay: RelayServer = self.server.relay  # type: ignore[attr-defined]
        resolver = relay.resolver_for(token)
        if resolver is None:
            self.send_error(404, "Unknown or expired token")
            return

        try:
            source = resolver()
        except Exception:
            log.exception("relay: resolver failed for token %s", token)
            self.send_error(502, "Failed to resolve stream")
            return

        request_headers = dict(source.headers)
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
                self._reply(upstream, source, send_body=send_body)
            except (BrokenPipeError, ConnectionResetError):
                # Device seeked or stopped mid-stream; nothing to do.
                pass

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
