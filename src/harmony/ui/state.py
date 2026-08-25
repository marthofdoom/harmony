"""Application-wide state shared by every page.

``AppState`` is constructed once by ``HarmonyApplication`` and passed down to
each page widget. It owns the backend singletons (settings, db, providers,
sync engine, recommender, planner) and is the single place that knows how to
degrade gracefully when a backend layer hasn't landed yet — every import of a
sibling layer is lazy and defensive so the UI stays launchable during
parallel development (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import logging
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GObject  # noqa: E402

from harmony.config import CredentialStore, Settings  # noqa: E402
from harmony.models import Playlist, Service  # noqa: E402
from harmony.tasks import run_async  # noqa: E402

log = logging.getLogger(__name__)


class AppState(GObject.Object):
    """Holds backend singletons and notifies pages of changes via signals."""

    __gsignals__ = {
        "providers-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "playlists-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "toast": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self.settings: Settings = Settings.load()
        self.credentials = CredentialStore()
        self.db: Any | None = self._open_db()
        self.providers: dict[Service, Any] = {}
        self.sync_engine: Any | None = None
        self.recommender: Any | None = None
        self.planner: Any | None = None

        self._playlist_cache: dict[Service, list[Playlist]] | None = None
        self._loading_playlists = False

        self.reload_providers()
        self._init_recommender()
        self._init_planner()

    # -- construction helpers ---------------------------------------------

    def _open_db(self) -> Any | None:
        """Open the sqlite database, tolerating db.py not existing yet."""
        try:
            from harmony.db import Database
        except ImportError as exc:
            log.warning("db layer unavailable: %s", exc)
            return None
        try:
            return Database()
        except Exception:
            log.exception("Failed to open database")
            return None

    def _build_providers(self) -> dict[Service, Any]:
        """Construct provider instances from settings/credentials.

        ``build_providers``'s exact signature is still being finalised by the
        providers layer; we try the documented shape first and fall back to a
        no-args call rather than crash the whole app on a signature mismatch.
        """
        try:
            from harmony.providers import build_providers
        except ImportError as exc:
            log.warning("providers layer unavailable: %s", exc)
            return {}
        try:
            return build_providers(self.settings, self.credentials)
        except TypeError:
            log.debug("build_providers(settings, credentials) rejected; retrying bare", exc_info=True)
            try:
                return build_providers()
            except Exception:
                log.exception("Failed to build providers")
                return {}
        except Exception:
            log.exception("Failed to build providers")
            return {}

    def _init_recommender(self) -> None:
        try:
            from harmony.enrich.recommender import Recommender
        except ImportError as exc:
            log.warning("recommender unavailable: %s", exc)
            self.recommender = None
            return
        try:
            self.recommender = Recommender(self.db, self.settings)
        except TypeError:
            try:
                self.recommender = Recommender()
            except Exception:
                log.exception("Failed to construct Recommender")
                self.recommender = None
        except Exception:
            log.exception("Failed to construct Recommender")
            self.recommender = None

    def _init_planner(self) -> None:
        try:
            from harmony.ai.claude import PlaylistPlanner
        except ImportError as exc:
            log.warning("AI planner unavailable: %s", exc)
            self.planner = None
            return
        try:
            from harmony.config import ANTHROPIC_API_KEY

            api_key = self.credentials.get(ANTHROPIC_API_KEY)
            self.planner = PlaylistPlanner(api_key=api_key, model=self.settings.ai_model)
        except Exception:
            log.exception("Failed to construct PlaylistPlanner")
            self.planner = None

    def _rebuild_sync_engine(self) -> None:
        if not self.providers or self.db is None:
            self.sync_engine = None
            return
        try:
            from harmony.sync import SyncEngine
        except ImportError as exc:
            log.warning("sync layer unavailable: %s", exc)
            self.sync_engine = None
            return
        try:
            self.sync_engine = SyncEngine(self.providers, self.db)
        except Exception:
            log.exception("Failed to construct SyncEngine")
            self.sync_engine = None

    # -- public API ---------------------------------------------------------

    def reload_providers(self) -> None:
        """Rebuild provider instances from current settings and notify pages."""
        self.providers = self._build_providers()
        self._playlist_cache = None
        self._rebuild_sync_engine()
        self.emit("providers-changed")

    def reload_planner(self) -> None:
        """Recreate the AI planner (e.g. after the API key changes in Preferences)."""
        self._init_planner()

    def all_playlists(self, refresh: bool = False) -> dict[Service, list[Playlist]]:
        """Return cached playlists, kicking off a background refresh as needed.

        Callers get the current cache immediately (empty on first call) and
        should listen for ``playlists-changed`` to redraw once the background
        fetch completes — this keeps the method synchronous and cheap while
        still honouring the "never block the main loop" rule.
        """
        if (refresh or self._playlist_cache is None) and not self._loading_playlists:
            self._loading_playlists = True

            def work() -> dict[Service, list[Playlist]]:
                result: dict[Service, list[Playlist]] = {}
                for service, provider in self.providers.items():
                    try:
                        result[service] = provider.list_playlists()
                    except Exception as exc:  # noqa: BLE001 - per-provider isolation
                        log.warning("Failed to list playlists for %s: %s", service, exc)
                        result[service] = []
                return result

            def done(result: dict[Service, list[Playlist]]) -> None:
                self._loading_playlists = False
                self._playlist_cache = result
                self.emit("playlists-changed")

            def error(exc: BaseException) -> None:
                self._loading_playlists = False
                self.toast(f"Couldn't load playlists: {exc}")

            run_async(work, done, error)
        return self._playlist_cache or {}

    def toast(self, text: str) -> None:
        """Emit a toast. Must be called from the main thread."""
        self.emit("toast", text)
