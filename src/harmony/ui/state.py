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
        self.provider_errors: dict[Service, str] = {}
        self.sync_engine: Any | None = None
        self.recommender: Any | None = None
        self.planner: Any | None = None

        self._playlist_cache: dict[Service, list[Playlist]] | None = None
        self._loading_playlists = False
        self._playlists_refresh_pending = False

        self._loading_providers = False
        self._providers_reload_pending = False

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

    def _build_providers(self) -> tuple[dict[Service, Any], dict[Service, str]]:
        """Construct provider instances from settings/credentials.

        Runs on a worker thread (see ``reload_providers``) because provider
        construction can perform real, blocking network I/O — a fresh Qobuz
        login scrapes play.qobuz.com plus a multi-MB bundle.js, each request
        with a 15s timeout. That must never happen on the GTK main loop.

        Returns ``(providers, errors)`` rather than raising, and builds each
        service independently: one provider failing to construct (e.g. Qobuz
        unreachable on an offline first launch) must degrade only that
        service, not wipe out every provider and take YouTube Music search,
        playlists, and sync down with it. ``errors`` carries a human-readable
        message per failed service for the UI to surface.

        This always goes through ``_build_providers_per_service`` rather than
        trying ``providers.build_providers()`` first: that documented entry
        point constructs both provider classes with no per-service try/except
        of its own, so it offers no real isolation, and it never calls
        ``_warm_up``. It's also documented to never raise for missing
        credentials (the common case), so a "try the atomic shape, fall back
        to per-service on failure" strategy made per-service construction —
        and the warm-up that lives there — effectively dead code on every
        normal launch. Going straight to per-service construction is what
        actually delivers the isolation and warm-up this method promises.
        """
        return self._build_providers_per_service()

    def _build_providers_per_service(self) -> tuple[dict[Service, Any], dict[Service, str]]:
        """Construct each provider class directly so a single failure is isolated."""
        try:
            from harmony.providers import QobuzProvider, YTMusicProvider
        except ImportError as exc:
            log.warning("providers layer unavailable: %s", exc)
            return {}, {}

        providers: dict[Service, Any] = {}
        errors: dict[Service, str] = {}
        for service, provider_cls in ((Service.YTMUSIC, YTMusicProvider), (Service.QOBUZ, QobuzProvider)):
            try:
                providers[service] = provider_cls(self.settings, self.credentials)
            except Exception as exc:  # noqa: BLE001 - per-provider isolation is the point
                log.warning("Failed to construct %s provider: %s", service, exc)
                errors[service] = str(exc) or exc.__class__.__name__
        self._warm_up(providers, errors)
        return providers, errors

    @staticmethod
    def _warm_up(providers: dict[Service, Any], errors: dict[Service, str]) -> None:
        """Establish sessions so the account rows reflect reality.

        Provider constructors are deliberately pure — no network, no keyring —
        so ``is_authenticated`` reads False for an already-configured account
        until something makes the first real call. This runs on the worker
        thread that built them, so the sign-in happens before the UI asks.
        A failure here is not fatal: it just means that service shows as
        disconnected, which is exactly what it is.
        """
        for service, provider in providers.items():
            try:
                if provider.is_authenticated:
                    continue
                provider.authenticate()
            except Exception as exc:  # noqa: BLE001 - unconfigured is the common case
                log.debug("Could not warm up %s: %s", service, exc)
                errors.setdefault(service, str(exc) or exc.__class__.__name__)

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

    def apply_matching_settings(self) -> None:
        """Re-read match thresholds and auto-accept into the live sync engine.

        The engine takes these by value at construction, so editing them in
        Preferences would otherwise not take effect until the next launch.
        """
        self._rebuild_sync_engine()

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
            self.sync_engine = SyncEngine(
                self.providers,
                self.db,
                high_threshold=self.settings.match_high_threshold,
                low_threshold=self.settings.match_low_threshold,
                auto_accept_high=self.settings.auto_accept_high,
            )
        except Exception:
            log.exception("Failed to construct SyncEngine")
            self.sync_engine = None

    # -- public API ---------------------------------------------------------

    def reload_providers(self) -> None:
        """Rebuild provider instances from current settings and notify pages.

        Construction happens off the main loop (``_build_providers`` can do
        real network I/O) and results are marshalled back via ``run_async``,
        which needs a running GLib main loop to deliver its callback — that's
        satisfied here because ``AppState`` is built during ``do_startup``,
        before ``Gio.Application.run()`` starts pumping the loop, and
        ``GLib.idle_add`` sources queued early just fire once it does.

        Concurrent calls (e.g. a debounced Preferences edit firing while the
        previous reload is still in flight) are coalesced rather than kicking
        off overlapping builds that could finish out of order and clobber
        each other's result.
        """
        self._playlist_cache = None
        if self._loading_providers:
            self._providers_reload_pending = True
            return
        self._loading_providers = True
        self._providers_reload_pending = False

        def work() -> tuple[dict[Service, Any], dict[Service, str]]:
            return self._build_providers()

        def finish(providers: dict[Service, Any], errors: dict[Service, str]) -> None:
            self._loading_providers = False
            self.providers = providers
            self.provider_errors = errors
            self._rebuild_sync_engine()
            self.emit("providers-changed")
            # The provider set just changed (most commonly: it went from
            # empty at startup to populated once the worker thread finishes,
            # *after* every page has already been constructed and made its
            # own now-stale ``all_playlists()`` call against an empty
            # ``self.providers``). Nothing else reloads playlists when that
            # happens, so without this the playlist cache — and every page
            # reading it — would stay empty for the rest of the session.
            # ``all_playlists`` already coalesces with any load in flight.
            self.all_playlists(refresh=True)
            if self._providers_reload_pending:
                self.reload_providers()

        def done(result: tuple[dict[Service, Any], dict[Service, str]]) -> None:
            finish(*result)

        def error(exc: BaseException) -> None:
            log.exception("Failed to build providers: %s", exc)
            finish({}, {})

        run_async(work, done, error)

    def reload_planner(self) -> None:
        """Recreate the AI planner (e.g. after the API key changes in Preferences)."""
        self._init_planner()

    def all_playlists(self, refresh: bool = False) -> dict[Service, list[Playlist]]:
        """Return cached playlists, kicking off a background refresh as needed.

        Callers get the current cache immediately (empty on first call) and
        should listen for ``playlists-changed`` to redraw once the background
        fetch completes — this keeps the method synchronous and cheap while
        still honouring the "never block the main loop" rule.

        A ``refresh=True`` that arrives while a load is already in flight
        (e.g. right after a create/rename, whose own ``done`` callback also
        calls ``all_playlists(refresh=True)``) used to be dropped silently —
        the guard below just returned the stale cache and never queued
        another fetch. That's coalesced now: the request is remembered and a
        fresh load starts as soon as the in-flight one finishes.
        """
        if self._loading_playlists:
            if refresh:
                self._playlists_refresh_pending = True
            return self._playlist_cache or {}
        if refresh or self._playlist_cache is None:
            self._start_playlist_load()
        return self._playlist_cache or {}

    def _start_playlist_load(self) -> None:
        self._loading_playlists = True
        self._playlists_refresh_pending = False

        def work() -> dict[Service, list[Playlist]]:
            result: dict[Service, list[Playlist]] = {}
            for service, provider in self.providers.items():
                try:
                    result[service] = provider.list_playlists()
                except Exception as exc:  # noqa: BLE001 - per-provider isolation
                    log.warning("Failed to list playlists for %s: %s", service, exc)
                    result[service] = []
            return result

        def finish() -> None:
            self._loading_playlists = False
            if self._playlists_refresh_pending:
                self._start_playlist_load()

        def done(result: dict[Service, list[Playlist]]) -> None:
            self._playlist_cache = result
            self.emit("playlists-changed")
            finish()

        def error(exc: BaseException) -> None:
            self.toast(f"Couldn't load playlists: {exc}")
            finish()

        run_async(work, done, error)

    def toast(self, text: str) -> None:
        """Emit a toast. Must be called from the main thread."""
        self.emit("toast", text)
