"""The headless web + API HTTP server (stdlib, threaded, GTK-free).

Serves the web client (static assets), the JSON API over the engine, and a
same-origin, Range-aware stream proxy so the browser's ``<audio>`` plays the
relay/provider stream directly. See ``docs/design/headless-server.md``.

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
from urllib.parse import parse_qs, unquote, urlparse

from harmony import __version__
from harmony.web.api import Engine

log = logging.getLogger(__name__)

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine


def _static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


class HarmonyHTTPRequestHandler(BaseHTTPRequestHandler):
    """Routes ``/healthz``, ``/api/*``, ``/stream/<token>``, and the static app."""

    server_version = f"Harmony/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        log.info("%s %s", self.address_string(), fmt % args)

    # -- response helpers ---------------------------------------------------

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

    def _authorized(self) -> bool:
        """True if the request may touch the API/stream (personal-key gate)."""
        parsed = urlparse(self.path)
        provided = self.headers.get("X-Harmony-Key")
        if provided is None:
            provided = (parse_qs(parsed.query).get("key") or [None])[0]
        return get_engine().check_key(provided)

    def _serve_static(self, path: str) -> None:
        static = _static_dir()
        if path in ("", "/"):
            path = "/index.html"
        target = (static / path.lstrip("/")).resolve()
        if (target == static or target.is_relative_to(static)) and target.is_file():
            self._send_file(target)
        else:
            self._send_json({"error": "not found"}, status=404)

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            self._send_json({"status": "ok", "service": "harmony", "version": __version__})
            return
        if path.startswith("/api/") or path.startswith("/stream/"):
            if not self._authorized():
                self._send_json({"error": "personal key required"}, status=401)
                return
            if path.startswith("/api/"):
                self._handle_api(path, parse_qs(parsed.query))
            else:
                self._handle_stream(unquote(path[len("/stream/"):]))
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._send_json({"error": "not found"}, status=404)
            return
        if not self._authorized():
            self._send_json({"error": "personal key required"}, status=401)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON body"}, status=400)
            return
        engine = get_engine()
        parts = [unquote(p) for p in parsed.path.strip("/").split("/")]
        try:
            if parts == ["api", "preferences"]:
                self._send_json(engine.set_preferences(personal_key=body.get("personal_key")))
            elif parts == ["api", "playlists"]:
                service = (body.get("service") or "").strip()
                title = (body.get("title") or "").strip()
                if not service or not title:
                    self._send_json({"error": "missing service or title"}, status=400)
                    return
                self._send_json(engine.create_playlist(service, title))
            elif len(parts) == 5 and parts[0:2] == ["api", "playlists"] and parts[4] in ("add", "remove"):
                ids = body.get("track_ids") or []
                op = engine.add_tracks if parts[4] == "add" else engine.remove_tracks
                self._send_json(op(parts[2], parts[3], ids))
            elif len(parts) == 5 and parts[0:2] == ["api", "playlists"] and parts[4] == "rename":
                title = (body.get("title") or "").strip()
                if not title:
                    self._send_json({"error": "missing title"}, status=400)
                    return
                self._send_json(engine.rename_playlist(parts[2], parts[3], title))
            elif len(parts) == 5 and parts[0:2] == ["api", "playlists"] and parts[4] == "delete":
                self._send_json(engine.delete_playlist(parts[2], parts[3]))
            elif parts == ["api", "sync", "plan"]:
                src, tgt = body.get("source") or {}, body.get("target") or {}
                if not src.get("service") or not src.get("id") or not tgt.get("service") or not tgt.get("id"):
                    self._send_json({"error": "missing source/target service+id"}, status=400)
                    return
                self._send_json(engine.sync_plan(src, tgt, body.get("direction") or "a_to_b"))
            elif parts == ["api", "sync", "apply"]:
                token = (body.get("token") or "").strip()
                if not token:
                    self._send_json({"error": "missing token"}, status=400)
                    return
                self._send_json(engine.sync_apply(token))
            elif len(parts) == 4 and parts[0:2] == ["api", "devices"] and parts[3] == "play":
                service = (body.get("service") or "").strip()
                track_id = (body.get("id") or "").strip()
                if not service or not track_id:
                    self._send_json({"error": "missing service or id"}, status=400)
                    return
                self._send_json(engine.cast(parts[2], service, track_id, body.get("meta") or {}))
            elif len(parts) == 4 and parts[0:2] == ["api", "devices"] and parts[3] in ("pause", "resume", "stop", "volume"):
                self._send_json(engine.device_control(parts[2], parts[3], body.get("level")))
            elif parts == ["api", "accounts", "qobuz", "token"]:
                token = (body.get("token") or "").strip()
                if not token:
                    self._send_json({"error": "missing token"}, status=400)
                    return
                self._send_json(engine.set_qobuz_token(token))
            elif parts == ["api", "accounts", "ytmusic", "browser"]:
                headers_raw = (body.get("headers") or "").strip()
                if not headers_raw:
                    self._send_json({"error": "missing headers"}, status=400)
                    return
                self._send_json(engine.set_ytmusic_browser(headers_raw))
            elif len(parts) == 4 and parts[0:2] == ["api", "accounts"] and parts[3] == "signout":
                self._send_json(engine.signout(parts[2]))
            else:
                self._send_json({"error": "not found"}, status=404)
        except KeyError as exc:
            self._send_json({"error": f"unknown service {exc}"}, status=404)
        except Exception as exc:  # noqa: BLE001 - surface as JSON, don't 500-crash
            log.warning("API POST error on %s: %s", parsed.path, exc)
            self._send_json({"error": str(exc)}, status=502)

    def _handle_api(self, path: str, query: dict[str, list[str]]) -> None:
        engine = get_engine()
        parts = [unquote(p) for p in path.strip("/").split("/")]  # ["api", ...]
        try:
            if parts == ["api", "accounts"]:
                self._send_json(engine.accounts())
            elif parts == ["api", "preferences"]:
                self._send_json(engine.preferences())
            elif parts == ["api", "search"]:
                q = (query.get("q") or [""])[0].strip()
                if not q:
                    self._send_json({"error": "missing q"}, status=400)
                    return
                kinds = tuple((query.get("kinds") or ["tracks"])[0].split(","))
                self._send_json(engine.search(q, kinds))
            elif parts == ["api", "playlists"]:
                self._send_json(engine.playlists())
            elif len(parts) == 5 and parts[0:2] == ["api", "playlists"] and parts[4] == "tracks":
                self._send_json(engine.playlist_tracks(parts[2], parts[3]))
            elif parts == ["api", "devices"]:
                self._send_json(engine.devices())
            elif len(parts) == 4 and parts[0:2] == ["api", "devices"] and parts[3] == "status":
                self._send_json(engine.device_status(parts[2]))
            elif parts == ["api", "resolve"]:
                service = (query.get("service") or [""])[0]
                track_id = (query.get("id") or [""])[0]
                if not service or not track_id:
                    self._send_json({"error": "missing service or id"}, status=400)
                    return
                self._send_json(engine.resolve(service, track_id))
            else:
                self._send_json({"error": "not found"}, status=404)
        except KeyError as exc:
            self._send_json({"error": f"unknown service {exc}"}, status=404)
        except Exception as exc:  # noqa: BLE001 - surface engine errors as JSON, don't 500-crash
            log.warning("API error on %s: %s", path, exc)
            self._send_json({"error": str(exc)}, status=502)

    def _handle_stream(self, token: str) -> None:
        import requests

        meta = get_engine().stream_for(token)
        if meta is None:
            self._send_json({"error": "unknown or expired stream token"}, status=404)
            return
        headers = dict(meta["headers"])
        client_range = self.headers.get("Range")
        if client_range:
            headers["Range"] = client_range
        try:
            upstream = requests.get(meta["url"], headers=headers, stream=True, timeout=20)
        except Exception as exc:  # noqa: BLE001
            log.warning("stream upstream failed: %s", exc)
            self._send_json({"error": "upstream fetch failed"}, status=502)
            return
        with upstream:
            self.send_response(upstream.status_code)
            has_length = "Content-Length" in upstream.headers
            for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                if header in upstream.headers:
                    self.send_header(header, upstream.headers[header])
            if "Content-Type" not in upstream.headers:
                self.send_header("Content-Type", meta["mime"])
            if not has_length:
                # No length → can't keep-alive under HTTP/1.1; close after body.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            try:
                for chunk in upstream.iter_content(64 * 1024):
                    if chunk:
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # Browser seeked or closed the <audio> element; expected.
                self.close_connection = True


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
