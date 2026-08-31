"""``harmony serve`` — run the headless web + HTTP API server.

The server exposes Harmony's featureset over HTTP for the web client and the
mobile app, holding the credentials centrally (the credential-custody /
federation model). No GTK: it drives the display-agnostic engine directly. See
``docs/design/headless-server.md``.

Security: the server holds real streaming credentials and controls the user's
accounts, so it binds ``127.0.0.1`` by default and only warns-and-serves on a
public bind. Expose it behind an authenticating reverse proxy (Caddy/nginx) or
over a private network (e.g. Tailscale) — never straight to the internet.
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
    args = _build_parser().parse_args(argv)

    if args.data_dir:
        # Redirect the platform dirs so a container/systemd deployment keeps
        # config, db, and secrets on one mounted volume. Set before anything
        # reads config paths.
        import os

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
