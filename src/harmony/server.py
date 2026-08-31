"""``harmony serve`` — run the GTK GUI headless, reachable from a browser.

Uses GTK's built-in **Broadway** HTML5 backend: ``gtk4-broadwayd`` provides an
in-browser display over a websocket, and the normal Harmony GTK application
connects to it (``GDK_BACKEND=broadway``) instead of X/Wayland. No separate
frontend and no engine changes -- the browser shows the real app. See
``docs/design/headless-server.md``.

Security: ``broadwayd`` has **no authentication of its own** and, left to its
defaults, binds every interface. So this binds ``127.0.0.1`` by default and only
warns-and-serves on a public bind -- expose it behind an authenticating reverse
proxy (Caddy/nginx) or over a private network (e.g. Tailscale), never straight to
the internet.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import socket
import subprocess
import time

log = logging.getLogger(__name__)


def _find_broadwayd() -> str | None:
    return shutil.which("gtk4-broadwayd") or shutil.which("broadwayd")


def _wait_listening(host: str, port: int, timeout: float = 10.0) -> bool:
    """Poll until ``host:port`` accepts a TCP connection (broadwayd is up)."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            try:
                sock.connect((probe_host, port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harmony serve",
        description="Serve the Harmony GUI to a browser via GTK's Broadway backend.",
    )
    parser.add_argument("--port", type=int, default=8085, help="browser HTTP port (default 8085)")
    parser.add_argument(
        "--address",
        default="127.0.0.1",
        help="interface broadwayd binds to (default 127.0.0.1). broadwayd has no "
        "auth: only expose it behind an authenticating proxy or a private network.",
    )
    parser.add_argument("--display", default=":5", help="Broadway display id (default :5)")
    parser.add_argument("--cert", help="TLS certificate path (serves https when set with --key)")
    parser.add_argument("--key", help="TLS key path")
    return parser


def serve(argv: list[str]) -> int:
    """Launch broadwayd, point the GTK app at it, and run until exit."""
    args = _build_parser().parse_args(argv)

    broadwayd = _find_broadwayd()
    if broadwayd is None:
        log.error(
            "gtk4-broadwayd not found. Install the GTK4 Broadway backend "
            "(Fedora: gtk4; Debian: libgtk-4-bin; Arch: gtk4)."
        )
        return 1

    cmd = [broadwayd, "--port", str(args.port), "--address", args.address]
    tls = bool(args.cert and args.key)
    if tls:
        cmd += ["--cert", args.cert, "--key", args.key]
    cmd.append(args.display)

    log.info("Starting Broadway display: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd)
    try:
        if not _wait_listening(args.address, args.port):
            log.error("broadwayd did not start listening on %s:%s", args.address, args.port)
            return 1
        scheme = "https" if tls else "http"
        log.info("Harmony GUI ready at %s://%s:%s/  (Broadway display %s)",
                 scheme, args.address, args.port, args.display)
        if args.address in ("0.0.0.0", "::", ""):
            log.warning(
                "broadwayd is bound to a public interface and has NO authentication. "
                "Put it behind an authenticating reverse proxy or restrict it to a "
                "private network (e.g. Tailscale) before exposing it."
            )
        os.environ["GDK_BACKEND"] = "broadway"
        os.environ["BROADWAY_DISPLAY"] = args.display
        return _run_app()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _run_app() -> int:
    """Run the normal GTK application (now on the Broadway backend)."""
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")

    from harmony.app import build_application

    # Don't forward the ``serve`` argv to GApplication -- it has already been
    # consumed here; pass a bare program name so GTK sees no unknown options.
    return build_application().run(["harmony"])
