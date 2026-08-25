"""Entry point: ``python -m harmony`` (also wired up as the ``harmony`` script)."""

from __future__ import annotations

import logging
import os
import sys


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("HARMONY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
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
