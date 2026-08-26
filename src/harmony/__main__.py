"""Entry point: ``python -m harmony`` (also wired up as the ``harmony`` script)."""

from __future__ import annotations

import logging
import os
import sys


def _log_file_handler() -> logging.Handler | None:
    """A rotating log file under the user cache dir, so crashes/errors are easy
    to retrieve (in the Flatpak: ~/.var/app/<app-id>/cache/harmony/harmony.log).
    Best effort -- never let logging setup break startup."""
    try:
        import pathlib
        from logging.handlers import RotatingFileHandler

        import platformdirs

        directory = pathlib.Path(platformdirs.user_cache_dir("harmony"))
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "harmony.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
        return handler
    except Exception:  # noqa: BLE001 - logging must never crash the app
        return None


def main() -> int:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    file_handler = _log_file_handler()
    if file_handler is not None:
        handlers.append(file_handler)
    logging.basicConfig(
        level=os.environ.get("HARMONY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
    )

    # Must happen before the first ``from gi.repository import Gtk/Adw`` anywhere
    # in the process, which is why this stays first in the entry point rather
    # than living in a module that might get imported some other way first.
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")

    from harmony.app import build_application

    app = build_application()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
