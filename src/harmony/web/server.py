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

# Pin the content-types the PWA depends on. A service worker is refused unless
# its script is served with a JS type, and `.webmanifest` is unknown to the
# stdlib map on some minimal hosts (→ application/octet-stream) — register both
# explicitly so the app installs regardless of the host's /etc/mime.types.
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")

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
            if path == "/api/audio/monitor":
                self._handle_monitor()
            elif path.startswith("/api/"):
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
                self._send_json(engine.cast(parts[2], service, track_id, body.get("meta") or {},
                                            via=body.get("via") or None))
            elif len(parts) == 4 and parts[0:2] == ["api", "devices"] and parts[3] in ("pause", "resume", "stop", "volume"):
                self._send_json(engine.device_control(parts[2], parts[3], body.get("level"),
                                                       via=body.get("via") or None))
            elif parts == ["api", "audio", "receive"]:
                self._send_json(engine.audio_receive(body.get("sink"), int(body.get("latency_ms") or 150)))
            elif parts == ["api", "audio", "send"]:
                to_host = (body.get("to_host") or "").strip()
                if not to_host:
                    self._send_json({"error": "missing to_host"}, status=400)
                    return
                self._send_json(engine.audio_send(to_host, int(body.get("latency_ms") or 150),
                                                  transport=body.get("transport")))
            elif parts == ["api", "audio", "stop"]:
                self._send_json(engine.audio_stop())
            elif parts == ["api", "peers"]:
                host = (body.get("host") or "").strip()
                port = body.get("port")
                if not host or not port:
                    self._send_json({"error": "missing host or port"}, status=400)
                    return
                self._send_json(engine.add_peer(host, int(port), name=body.get("name")))
            elif parts == ["api", "peers", "remove"]:
                host = (body.get("host") or "").strip()
                port = body.get("port")
                if not host or not port:
                    self._send_json({"error": "missing host or port"}, status=400)
                    return
                self._send_json(engine.remove_peer(host, int(port)))
            elif parts == ["api", "credentials", "adopt"]:
                host = (body.get("host") or "").strip()
                port = body.get("port")
                if host and port:
                    self._send_json(engine.adopt_from_peer(host, int(port)))
                else:  # no peer given → auto-adopt from any key-matching peer
                    self._send_json(engine.maybe_adopt_credentials())
            elif parts == ["api", "audio", "route"]:
                direction = (body.get("direction") or "").strip()
                peer_host = (body.get("peer_host") or "").strip()
                peer_port = body.get("peer_port")
                if direction not in ("send", "receive") or not peer_host or not peer_port:
                    self._send_json({"error": "need direction (send|receive), peer_host, peer_port"}, status=400)
                    return
                self._send_json(engine.audio_route(direction, peer_host, int(peer_port),
                                                    body.get("sink"), int(body.get("latency_ms") or 150)))
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
            elif parts == ["api", "accounts", "ytmusic", "autodetect"]:
                self._send_json(engine.ytmusic_autodetect(body.get("browser")))
            elif parts == ["api", "accounts", "ytmusic", "oauth", "client"]:
                cid = (body.get("client_id") or "").strip()
                secret = (body.get("client_secret") or "").strip()
                if not cid or not secret:
                    self._send_json({"error": "missing client_id or client_secret"}, status=400)
                    return
                self._send_json(engine.set_ytmusic_oauth_client(cid, secret))
            elif parts == ["api", "accounts", "ytmusic", "oauth", "start"]:
                self._send_json(engine.ytmusic_oauth_start())
            elif parts == ["api", "accounts", "ytmusic", "oauth", "poll"]:
                token = (body.get("poll_token") or "").strip()
                if not token:
                    self._send_json({"error": "missing poll_token"}, status=400)
                    return
                self._send_json(engine.ytmusic_oauth_poll(token))
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
            elif parts == ["api", "instances"]:
                self._send_json(engine.instances())
            elif parts == ["api", "preferences"]:
                self._send_json(engine.preferences())
            elif parts == ["api", "search"]:
                q = (query.get("q") or [""])[0].strip()
                if not q:
                    self._send_json({"error": "missing q"}, status=400)
                    return
                kinds = tuple((query.get("kinds") or ["tracks"])[0].split(","))
                self._send_json(engine.search(q, kinds))
            elif parts == ["api", "search", "smart"]:
                q = (query.get("q") or [""])[0].strip()
                if not q:
                    self._send_json({"error": "missing q"}, status=400)
                    return
                svc = (query.get("service") or ["both"])[0]
                self._send_json(engine.search_smart(q, service=svc))
            elif len(parts) == 4 and parts[0:2] == ["api", "artist"]:
                self._send_json(engine.artist_page(parts[2], parts[3]))
            elif len(parts) == 4 and parts[0:2] == ["api", "album"]:
                self._send_json(engine.album_page(parts[2], parts[3]))
            elif len(parts) == 4 and parts[0:2] == ["api", "track"]:
                self._send_json(engine.track_page(parts[2], parts[3]))
            elif parts == ["api", "playlists"]:
                self._send_json(engine.playlists())
            elif len(parts) == 5 and parts[0:2] == ["api", "playlists"] and parts[4] == "tracks":
                self._send_json(engine.playlist_tracks(parts[2], parts[3]))
            elif parts == ["api", "devices"]:
                refresh = (query.get("refresh") or ["0"])[0] in ("1", "true", "yes")
                if (query.get("peers") or ["0"])[0] in ("1", "true", "yes"):
                    self._send_json(engine.federated_devices(refresh=refresh))
                else:
                    self._send_json(engine.devices(refresh=refresh))
            elif parts == ["api", "peers"]:
                self._send_json(engine.instances())
            elif len(parts) == 4 and parts[0:2] == ["api", "devices"] and parts[3] == "status":
                via = (query.get("via") or [""])[0] or None
                self._send_json(engine.device_status(parts[2], via=via))
            elif parts == ["api", "credentials", "export"]:
                # Sensitive: only a key-matching caller reaches here (the gate),
                # and never an open instance — refuse when no key is set.
                from harmony.config import Settings

                if not Settings.load().personal_key:
                    self._send_json({"error": "set a personal key to enable credential sharing"},
                                    status=403)
                    return
                self._send_json(engine.export_credentials())
            elif parts == ["api", "audio", "sinks"]:
                self._send_json(engine.audio_sinks())
            elif parts == ["api", "audio", "status"]:
                self._send_json(engine.audio_status())
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

    def _handle_monitor(self) -> None:
        """Stream this machine's live audio output as MP3 (ffmpeg from the
        default sink's monitor). A light client pulls this to play the hub's
        audio — reliable over any network, unlike pushed UDP."""
        import subprocess

        argv = get_engine().audio_monitor_argv()
        if argv is None:
            self._send_json({"error": "ffmpeg or a default sink is unavailable"}, status=503)
            return
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.warning("monitor ffmpeg failed to start: %s", exc)
            self._send_json({"error": "couldn't start the audio capture"}, status=502)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client stopped listening
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _handle_stream(self, token: str) -> None:
        import requests

        meta = get_engine().stream_for(token)
        if meta is None:
            self._send_json({"error": "unknown or expired stream token"}, status=404)
            return
        client_range = self.headers.get("Range")

        def fetch(m: dict[str, object]) -> object:
            headers = dict(m["headers"])
            if client_range:
                headers["Range"] = client_range
            return requests.get(m["url"], headers=headers, stream=True, timeout=20)

        try:
            upstream = fetch(meta)
            # A CDN URL signed for ~minutes can expire before a late seek; the
            # provider then 403/410s. Re-resolve the token once and retry.
            if upstream.status_code in (403, 410):
                refreshed = get_engine().refresh_stream(token)
                if refreshed is not None:
                    upstream.close()
                    upstream = fetch(refreshed)
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


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    """Create the HTTP server and start the LAN mesh (advertise + discover)."""
    httpd = ThreadingHTTPServer((host, port), HarmonyHTTPRequestHandler)
    httpd.daemon_threads = True
    get_engine().start_mesh(port, bind_host=host)  # best-effort; skips a loopback bind
    _schedule_credential_adoption()
    return httpd


def _schedule_credential_adoption() -> None:
    """A full instance with a matching key but no accounts of its own pulls
    credentials from a peer once discovery has found one — so a fresh headless
    server becomes a working credential holder without manual onboarding."""
    import threading
    import time

    def run() -> None:
        time.sleep(6)  # give mDNS discovery a moment to populate peers
        try:
            get_engine().maybe_adopt_credentials()
        except Exception:  # noqa: BLE001 - best-effort
            log.debug("startup credential adoption failed", exc_info=True)

    threading.Thread(target=run, daemon=True, name="harmony-adopt").start()


def serve(host: str, port: int) -> int:
    """Run the web server until interrupted (the ``harmony serve`` CLI path)."""
    httpd = make_server(host, port)
    log.info("Harmony server listening on http://%s:%s/", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def start_background(host: str, port: int) -> ThreadingHTTPServer:
    """Start the server on a daemon thread and return the httpd (for shutdown).

    Used by the desktop app so a desktop instance is also a server + mesh node.
    """
    import threading

    httpd = make_server(host, port)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="harmony-web").start()
    log.info("Harmony server (embedded) listening on http://%s:%s/", host, port)
    return httpd
