"""``harmony serve`` — run the headless web + HTTP API server.

The server exposes Harmony's featureset over HTTP for the web client and the
mobile app, holding the credentials centrally (the credential-custody /
federation model). No GTK: it drives the display-agnostic engine directly. See
``docs/design/headless-server.md``.

Security: the server holds real streaming credentials and controls the user's
accounts. It binds ``0.0.0.0`` by default (reachable-by-default is the intended
UX — the mobile app and other instances need to reach it), gated by the optional
personal key and warning when it serves on a public interface. Expose it behind
an authenticating reverse proxy (Caddy/nginx) or over a private network (e.g.
Tailscale) — never straight to the internet.
"""

from __future__ import annotations

import argparse
import logging

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harmony serve",
        description="Run the Harmony web + HTTP API server (headless).",
    )
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default 8080)")
    parser.add_argument(
        "--address",
        default="0.0.0.0",  # noqa: S104 - reachable-by-default is the intended UX
        help="interface to bind (default 0.0.0.0, all interfaces). Set 127.0.0.1 "
        "to restrict to localhost. The server has no auth by default -- keep it on "
        "a trusted network (LAN/Tailnet) or behind an authenticating proxy.",
    )
    parser.add_argument(
        "--data-dir",
        help="override the config/data directory (default: platform user dirs). "
        "Point this at a mounted volume in a container.",
    )
    return parser


def _is_public_bind(address: str) -> bool:
    return address in ("0.0.0.0", "::", "")


def serve(argv: list[str]) -> int:
    import os

    args = _build_parser().parse_args(argv)

    # A headless server has no unlocked login keyring. If the `keyring` library
    # half-detects a Secret Service backend it can't actually use (locked, no
    # session), writes/reads silently drop credentials — so a copied token looks
    # saved but never authenticates. Default the server to the 0600 file store,
    # which persists reliably under --data-dir. The desktop GUI (not `serve`)
    # keeps using the real keyring. An explicit env var still wins.
    os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")

    if args.data_dir:
        # Redirect the platform dirs so a container/systemd deployment keeps
        # config, db, and secrets on one mounted volume. Set before anything
        # reads config paths.
        os.environ.setdefault("XDG_CONFIG_HOME", args.data_dir)
        os.environ.setdefault("XDG_DATA_HOME", args.data_dir)
        os.environ.setdefault("XDG_CACHE_HOME", args.data_dir)

    # Auth is off by default and the server binds all interfaces by default, so
    # the network is the security boundary. One concise notice; not a blocker.
    if _is_public_bind(args.address):
        log.warning(
            "No authentication and bound to all interfaces (%s:%s). Keep this on a "
            "trusted network (LAN/Tailnet) or behind an authenticating proxy.",
            args.address or "::", args.port,
        )

    from harmony.web.server import serve as web_serve

    return web_serve(args.address, args.port)
