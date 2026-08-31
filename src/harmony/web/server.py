"""The headless web + API HTTP server (stdlib, threaded, GTK-free).

Slice 1: a health endpoint and the static web-app shell. The API endpoints
(search, resolve/stream, playlists, sync, devices) land in subsequent slices —
each a thin wrapper over the engine modules, which are already display-agnostic.

Deliberately stdlib (``http.server``): zero heavy dependencies keeps the
container/AUR/.deb builds trivial and the whole thing unit-testable headless. A
threaded server is ample for a single-user home hub; an ASGI framework can come
later without changing the handlers' shape.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harmony import __version__

log = logging.getLogger(__name__)


def _static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


class HarmonyHTTPRequestHandler(BaseHTTPRequestHandler):
    """Routes ``/healthz`` + the static app shell; 404 otherwise (for now)."""

    server_version = f"Harmony/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        log.info("%s %s", self.address_string(), fmt % args)

    def _send_json(self, obj: object, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path: str) -> None:
        static = _static_dir()
        if path in ("", "/"):
            path = "/index.html"
        target = (static / path.lstrip("/")).resolve()
        # Path-traversal guard: the resolved file must live under the static dir.
        if (target == static or target.is_relative_to(static)) and target.is_file():
            self._send_file(target)
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json({"status": "ok", "service": "harmony", "version": __version__})
            return
        if path.startswith("/api/"):
            self._send_json({"error": "not implemented"}, status=501)
            return
        self._serve_static(path)


def serve(host: str, port: int) -> int:
    """Run the web server until interrupted."""
    httpd = ThreadingHTTPServer((host, port), HarmonyHTTPRequestHandler)
    httpd.daemon_threads = True
    log.info("Harmony server listening on http://%s:%s/", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
