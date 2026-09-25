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
import time
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, GObject  # noqa: E402

from harmony.config import CredentialStore, Settings  # noqa: E402
from harmony.models import Playlist, Service  # noqa: E402
from harmony.playqueue import PlayQueue  # noqa: E402
from harmony.tasks import on_main, run_async  # noqa: E402


@dataclass
class PlaybackState:
    """App-wide 'what's playing right now' model, driven to the Now Playing bar
    and the on-screen now-playing indicators via the ``playback-changed`` signal.

    One active playback device at a time (``active_host``). ``track`` is the
    current track object; ``collection_key`` is the ``(service, id)`` of the
    album/playlist it came from, or ``None`` for a single track. Positions are
    in seconds. ``repeat`` is ``"off" | "all" | "one"``.
    """

    active_host: str | None = None
    track: Any | None = None
    collection_key: tuple[Service, str] | None = None
    state: str = "stopped"  # playing | paused | stopped | unknown
    position_s: int | None = None
    duration_s: int | None = None
    volume: int | None = None
    volume_supported: bool = False
    shuffle: bool = False
    repeat: str = "off"
    has_prev: bool = False
    has_next: bool = False

    def track_key(self) -> tuple[Service, str] | None:
        """``(service, id)`` of the current track, for row-indicator matching."""
        if self.track is None:
            return None
        return (self.track.service, self.track.id)

    def is_active(self) -> bool:
        return self.track is not None and self.state in ("playing", "paused", "loading")

# Synthetic host id for the in-app local player ("This computer"). Routed to a
# GStreamer LocalPlayer instead of the relay + a network device.
LOCAL_HOST = "__local__"

log = logging.getLogger(__name__)


class AppState(GObject.Object):
    """Holds backend singletons and notifies pages of changes via signals."""

    __gsignals__ = {
        "providers-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "playlists-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # A specific playlist's *track contents* changed (a track was added to
        # it from another page). Carries the mutated Playlist so an open track
        # view can reload itself — ``playlists-changed`` only refreshes the
        # playlist *list* (titles/counts), not the tracks pane.
        "playlist-tracks-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # The optional integrations (AI planner, recommender sources) were
        # reconfigured. Pages that render "not configured" placeholders must
        # listen for this, or those placeholders survive the user fixing the
        # very thing they complain about.
        "integrations-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # The known-devices list (add/remove/rename) changed; devices_page
        # rebuilds its list from ``known_devices()`` in response.
        "devices-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # The app-wide playback model (self.playback) changed: current track,
        # transport state, position/duration/volume, shuffle/repeat, or the
        # active device. The Now Playing bar and every on-screen now-playing
        # indicator subscribe to this.
        "playback-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
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

        # Lazily created the first time ``device_for`` needs one; shared so
        # every ``WiiMDevice`` a page constructs for the same session reuses
        # one connection pool instead of opening a fresh one per call.
        self._device_session: Any | None = None

        self._init_playback()

        self.reload_providers()
        # The in-process engine (serving phones/peers, the detail pages, device
        # queues) can change credentials behind our back — a sync pull, a peer
        # pushing, onboarding. Pick those up: re-read settings, rebuild providers.
        try:
            self._engine().add_credentials_listener(
                lambda: on_main(self._on_engine_credentials_changed))
        except Exception:  # noqa: BLE001 - no engine is survivable (tests, partial installs)
            log.debug("engine credentials listener unavailable", exc_info=True)
        self._init_recommender()
        self._init_planner()
        self.restore_queue()
        self.connect("playback-changed", lambda *_a: self._schedule_queue_save())
        # Populate the output picker (LAN + peers' devices) once the mesh settles.
        GLib.timeout_add_seconds(8, self.discover_outputs)

    # -- construction helpers ---------------------------------------------

    def _init_playback(self) -> None:
        """Playback model + queue bookkeeping (separate so tests can build a bare state)."""
        # host -> (title, artist) last played there (the Devices page shows it).
        self._now_playing: dict[str, tuple[str, str]] = {}
        # The one active queue (see the playback section) + its bookkeeping.
        self.queue = PlayQueue()
        self._active_collection: tuple[Service, str] | None = None
        self._play_gen = 0          # bumps on every local start; stale resolves drop
        self._local_failures = 0
        self._poll_id: int | None = None
        self._poll_tick = 0
        self._poll_inflight = False
        self._last_remote_error: str | None = None
        self._peer_devices: list[Any] = []   # mesh peers' renderers ("via")
        # The one app-wide playback model the Now Playing bar reflects/controls.
        self.playback = PlaybackState()
        self._save_id: int | None = None
        self._resume_at = 0            # restored position to seek to on first Play
        self._remote_has_queue = False  # the device's own queue is loaded (not just mirrored)
        # After a seek, hold the optimistic position until the device/player
        # actually converges: an in-flight status poll started before the seek
        # would otherwise write the pre-seek position back and snap the bar (and
        # the perceived play head) back to where it was.
        self._seek_settle_until = 0.0
        self._seek_target_s = 0
        # The in-app GStreamer player ("This computer"); created on first use.
        self._local_player: Any | None = None

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

        Skips ``authenticate()`` entirely for a provider that reports no
        ``has_credentials`` (an I/O-free check) — a user with no account on
        that service would otherwise pay for a doomed authenticate() call on
        every launch and every debounced Preferences edit. ``authenticate()``
        itself still fails instantly with no I/O for an unconfigured
        provider, so this is belt-and-suspenders: it saves the call (and its
        log noise) rather than being the only thing preventing I/O.
        ``getattr(..., True)`` keeps this optional — a provider that doesn't
        define ``has_credentials`` is just always attempted, as before.
        """
        for service, provider in providers.items():
            try:
                if provider.is_authenticated:
                    continue
                if not getattr(provider, "has_credentials", True):
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

    def _on_engine_credentials_changed(self) -> None:
        self.settings.reload()
        self.reload_providers()

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
        # Keep the engine's provider set in step (it serves the detail pages and
        # device queues): a sign-in here must be seen there too.
        try:
            self._engine().reset_providers(notify=False)
        except Exception:  # noqa: BLE001 - engine optional here
            log.debug("engine provider reset failed", exc_info=True)
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
            # The provider set just changed (most commonly: it went from
            # empty at startup to populated once the worker thread finishes,
            # *after* every page has already been constructed and made its
            # own now-stale ``all_playlists()`` call against an empty
            # ``self.providers``). Nothing else reloads playlists when that
            # happens, so without this the playlist cache — and every page
            # reading it — would stay empty for the rest of the session.
            #
            # This must run *before* the ``providers-changed`` emit below,
            # not after: that signal is delivered synchronously to every
            # connected page, and at least one of them (search's
            # ``_refresh_playlist_choices``) reacts by calling its own
            # ``all_playlists()`` right there mid-emit. If the refresh below
            # ran after the emit, that in-emit call would see
            # ``_loading_playlists`` still False, start its own sweep, and
            # then this call would land on top of it while it's in flight —
            # coalesced via ``_playlists_refresh_pending`` rather than
            # dropped, but still two full ``list_playlists()`` passes across
            # every provider back to back. Doing it first means the in-emit
            # call instead finds a load already in flight and just reads the
            # (stale, soon to be replaced) cache; it still gets fresh data
            # via the ``playlists-changed`` signal once the single sweep
            # started here completes.
            self.all_playlists(refresh=True)
            self.emit("providers-changed")
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
        self.emit("integrations-changed")

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
            log.exception("Couldn't load playlists")
            self.toast("Couldn't load your playlists — check your connection.")
            finish()

        run_async(work, done, error)

    # -- playback devices ---------------------------------------------------
    #
    # Deliberately synchronous and network-free: these methods only ever read
    # or write ``self.settings.known_devices`` (a plain list of dicts) and
    # emit ``devices-changed``. Anything that talks to a device over HTTP
    # (status/play/pause/volume/discovery) belongs in devices_page.py, run
    # through ``harmony.tasks.run_async`` per the threading rule in
    # docs/ARCHITECTURE.md — never here.

    def known_devices(self) -> list[Any]:
        """Return ``settings.known_devices`` as ``harmony.playback.DeviceInfo``.

        Imports ``harmony.playback`` lazily (see ``device_for``) and degrades
        to an empty list if that layer isn't importable, matching how the
        rest of this class treats optional backend layers.
        """
        try:
            from harmony.playback import DeviceInfo
        except ImportError as exc:
            log.warning("playback layer unavailable: %s", exc)
            return []
        devices = []
        for entry in self.settings.known_devices:
            host = entry.get("host")
            if not host:
                continue
            devices.append(
                DeviceInfo(
                    id=host,
                    name=entry.get("name") or host,
                    host=host,
                    kind=entry.get("kind", "wiim"),
                )
            )
        return devices

    def playback_targets(self) -> list[Any]:
        """Known devices plus the synthetic 'This computer' local player, first.

        Used by the device pickers and the Now Playing bar so local playback is
        just another target. Returns ``harmony.playback.DeviceInfo`` entries.
        """
        try:
            from harmony.playback import DeviceInfo
        except ImportError:
            return self.known_devices()
        local = DeviceInfo(id=LOCAL_HOST, name="This computer", host=LOCAL_HOST, kind="local")
        return [local, *self.all_devices(), *self._peer_devices]

    def _get_local_player(self) -> Any:
        """Lazily create the GStreamer local player (main loop only)."""
        if self._local_player is None:
            from harmony.ui.local_player import LocalPlayer

            self._local_player = LocalPlayer(
                on_eos=self._on_local_eos,
                on_error=self._on_local_error,
            )
        return self._local_player

    def local_audio_label(self) -> str | None:
        """Negotiated output format of the in-app player while it's the active,
        playing target (e.g. ``"96 kHz · 24-bit · 2ch"``); None otherwise. Lets
        the Now Playing bar show what "This computer" is actually outputting."""
        if self.playback.active_host != LOCAL_HOST or self._local_player is None:
            return None
        try:
            return self._local_player.audio_info()
        except Exception:  # noqa: BLE001 - a caps read must never break the bar
            return None

    def add_device(self, host: str, name: str | None = None, kind: str = "wiim") -> None:
        """Add a device by host, deduped by host. No-op if already known."""
        host = host.strip()
        if not host:
            return
        if any(entry.get("host") == host for entry in self.settings.known_devices):
            return
        self.settings.known_devices.append(
            {"host": host, "name": (name or host).strip() or host, "kind": kind or "wiim"}
        )
        self.settings.save()
        self.emit("devices-changed")

    def _device_entry(self, host: str) -> dict[str, Any]:
        for entry in self.settings.known_devices:
            if entry.get("host") == host:
                return entry
        for d in getattr(self, "_discovered", ()):  # discovered-but-unsaved
            if d.host == host:
                return {"host": d.host, "name": d.name, "kind": d.kind}
        return {}

    def is_saved_device(self, host: str) -> bool:
        """True if ``host`` is a persisted device (vs a transient discovery)."""
        return any(entry.get("host") == host for entry in self.settings.known_devices)

    def set_discovered_devices(self, infos: list[Any]) -> None:
        """Cache auto-discovered devices so they appear in the device list and
        playback pickers without a manual add — mirroring the web Devices tab.
        Not persisted; ``add_device`` is how the user pins one."""
        self._discovered = list(infos)
        self.emit("devices-changed")

    def all_devices(self) -> list[Any]:
        """Saved devices plus the latest auto-discovered ones (deduped by host)."""
        known = self.known_devices()
        seen = {d.host for d in known}
        extra = [d for d in getattr(self, "_discovered", ()) if d.host not in seen]
        return [*known, *extra]

    def remove_device(self, host: str) -> None:
        """Forget a device. No-op if it wasn't known."""
        before = len(self.settings.known_devices)
        self.settings.known_devices = [e for e in self.settings.known_devices if e.get("host") != host]
        if len(self.settings.known_devices) != before:
            self.settings.save()
            self.emit("devices-changed")

    def set_device_name(self, host: str, name: str) -> None:
        """Persist a display name discovered from the device itself (getStatusEx).

        Called once a status/info fetch resolves the real device name for an
        entry that was added by host only (so it was showing the host as its
        name until now).
        """
        name = name.strip()
        changed = False
        for entry in self.settings.known_devices:
            if entry.get("host") == host and name and entry.get("name") != name:
                entry["name"] = name
                changed = True
        if changed:
            self.settings.save()
            self.emit("devices-changed")

    def device_for(self, host: str) -> Any:
        """Construct a ``WiiMDevice`` for ``host``.

        Imported lazily so a headless/no-GTK import of ``AppState`` (tests,
        or a future non-desktop frontend importing this module by mistake)
        never pays for ``harmony.playback`` — and so the constructor cost
        stays off the hot path of just building ``AppState``. Callers run
        this off the main loop via ``run_async``; construction itself does
        no I/O (``WiiMDevice.__init__`` is pure), only the methods called on
        the result do.
        """
        entry = self._device_entry(host)
        if entry.get("kind") == "cast":
            from harmony.playback import ChromecastDevice
            from harmony.playback.base import DeviceInfo

            info = DeviceInfo(id=host, name=entry.get("name") or host, host=host, kind="cast")
            return ChromecastDevice(host, info=info)

        from harmony.playback import device_from_host

        if self._device_session is None:
            import requests

            self._device_session = requests.Session()
        return device_from_host(host, session=self._device_session)

    # -- playback: one active queue, played here or handed to a device ---------
    #
    # One active stream per instance. ``self.queue`` (the shared PlayQueue — the
    # same rules the server, the web client and the phone use) is the active
    # queue. "This computer" plays it with GStreamer and advances on EOS. A
    # network device — on this LAN or a peer's ("via") — is driven by the
    # engine's server-owned device queue (``harmony.web.device_queue``), the SAME
    # queue a phone or browser controlling this instance sees; ``self.queue``
    # then mirrors that queue's snapshot. Everything here runs on the main loop;
    # only resolve/engine calls go to workers.

    @staticmethod
    def split_target(target: str) -> tuple[str, str | None]:
        """``"peerhost:port/devicehost"`` (a peer's device) -> (device host, via);
        a plain host -> (host, None)."""
        if "/" in target:
            via, host = target.split("/", 1)
            return host, via
        return target, None

    @staticmethod
    def _engine() -> Any:
        from harmony.web.server import get_engine

        return get_engine()

    def _is_remote(self) -> bool:
        host = self.playback.active_host
        return bool(host) and host != LOCAL_HOST

    def queue_index(self) -> int:
        return self.queue.index

    def active_queue(self) -> list[Any]:
        """The whole active queue (played, current, up next) — Now Playing's list."""
        return list(self.queue.tracks)

    def current_queue(self) -> list[Any]:
        return self.active_queue()

    # -- starting playback ------------------------------------------------------

    def play_list(self, tracks: list[Any], index: int = 0, host: str | None = None,
                  collection_key: tuple[Service, str] | None = None,
                  shuffle: bool = False) -> None:
        """Make ``tracks`` the active queue and play from ``index`` (main loop).

        ``shuffle=True`` is one-click shuffle play: turns shuffle on and starts
        at a random track. ``host`` defaults to the current output."""
        tracks = [t for t in tracks if t is not None]
        if not tracks:
            return
        host = host or self.playback.active_host or LOCAL_HOST
        self._switch_output(host)
        self._resume_at = 0
        self._active_collection = collection_key
        if shuffle:
            self.playback.shuffle = True
        start = None if shuffle else max(0, min(index, len(tracks) - 1))
        if host == LOCAL_HOST:
            idx = self.queue.load(tracks, start, shuffle=self.playback.shuffle)
            if idx is not None:
                self._start_local(idx)
            return
        # Show it straight away; the device's snapshot replaces this shortly.
        self.queue.load(tracks, start, shuffle=self.playback.shuffle)
        self._mark_now_playing(host, self.queue.current(), state="loading")
        self._remote_op("load", {
            "tracks": [self._track_dict(t) for t in tracks], "start": start,
            "shuffle": self.playback.shuffle, "repeat": self.playback.repeat,
        }, error="Couldn't play on that device.")

    def play_tracks_on_device(self, tracks: list[Any], device_host: str,
                              collection_key: tuple[Service, str] | None = None) -> None:
        """Thread-safe wrapper: play a list on ``device_host`` (from any thread)."""
        on_main(self.play_list, list(tracks), 0, device_host, collection_key)

    def play_track_on_device(self, track: Any, device_host: str) -> None:
        """Thread-safe wrapper: play one track on ``device_host`` (from any thread)."""
        on_main(self.play_list, [track], 0, device_host)

    @staticmethod
    def _track_dict(t: Any) -> dict[str, Any]:
        from harmony.web.api import track_to_dict

        d = track_to_dict(t)
        d["art_url"] = d.get("artwork_url")
        return d

    def _switch_output(self, host: str) -> None:
        """One active stream: stop the old output when playback moves to ``host``."""
        old = self.playback.active_host
        if old and old != host:
            self._halt_output(old)
            self.queue = self._fresh_queue()
        self.playback.active_host = host
        self._ensure_poller()

    def _fresh_queue(self) -> PlayQueue:
        q = PlayQueue()
        q.shuffle, q.repeat = self.playback.shuffle, self.playback.repeat
        return q

    def _halt_output(self, target: str) -> None:
        if target == LOCAL_HOST:
            self._play_gen += 1  # drop any in-flight resolve
            self._stop_local_player()
            return
        host, via = self.split_target(target)
        eng = self._engine()
        run_async(lambda: eng.device_queue_op(host, "stop", {}, via=via), None,
                  lambda exc: log.debug("stopping %s failed: %s", target, exc))

    # -- local ("This computer") -------------------------------------------------

    def _start_local(self, index: int) -> None:
        if self.queue.jump(index) is None:
            return
        track = self.queue.current()
        self._play_gen += 1
        gen = self._play_gen
        self._seek_settle_until = 0.0
        self._mark_now_playing(LOCAL_HOST, track, state="loading")
        provider = self.providers.get(track.service)

        def work() -> Any:
            if provider is None:
                raise RuntimeError(f"No provider configured for {track.service.label}")
            # The in-app player decodes locally, so ask for the highest tier.
            return provider.resolve_stream(track.id, max_quality=True)

        def done(source: Any) -> None:
            if gen != self._play_gen:
                return  # superseded by a newer play/skip while resolving
            log.info("Local playback: %s (%s)", getattr(source, "label", "?"), source.mime_type)
            self._get_local_player().load_and_play(source.url, dict(source.headers))
            self._local_failures = 0
            self.playback.state = "playing"
            self.emit("playback-changed")

        def failed(exc: BaseException) -> None:
            if gen == self._play_gen:
                self._local_failed(exc)

        run_async(work, done, failed)

    def _local_failed(self, exc: BaseException | str) -> None:
        """A local track couldn't play: say so, then move on (never spin forever)."""
        track = self.queue.current()
        self.toast(f"Couldn't play “{getattr(track, 'title', 'track')}”: {exc}")
        self._local_failures += 1
        nxt = self.queue.following(manual=True)
        if nxt is None or self._local_failures >= len(self.queue.tracks):
            self._local_failures = 0
            self._end_playback()
            return
        self._start_local(nxt)

    def _on_local_eos(self) -> None:
        """A locally-played track ended: advance the queue or stop (main loop)."""
        if self.playback.active_host != LOCAL_HOST:
            return
        nxt = self.queue.advance()
        if nxt is None:
            self._end_playback()
        else:
            self._start_local(nxt)

    def _on_local_error(self, msg: str) -> None:
        if self.playback.active_host == LOCAL_HOST:
            self._local_failed(msg)

    # -- devices (engine-owned queue) ------------------------------------------

    def _remote_op(self, op: str, body: dict[str, Any] | None = None,
                   error: str = "Couldn't control playback.") -> None:
        target = self.playback.active_host
        if not target or target == LOCAL_HOST:
            return
        host, via = self.split_target(target)
        eng = self._engine()
        run_async(lambda: eng.device_queue_op(host, op, body or {}, via=via),
                  lambda snap: self._apply_snapshot(target, snap),
                  lambda exc: self._toast_playback_error(exc, error))

    def _remote_control(self, action: str, level: int | None = None,
                        error: str = "Couldn't control playback.") -> None:
        target = self.playback.active_host
        if not target or target == LOCAL_HOST:
            return
        host, via = self.split_target(target)
        eng = self._engine()
        run_async(lambda: eng.device_control(host, action, level, via=via), None,
                  lambda exc: self._toast_playback_error(exc, error))

    def _apply_snapshot(self, target: str, snap: dict[str, Any]) -> None:
        """Mirror a device queue snapshot into ``self.queue`` + ``self.playback``."""
        if target != self.playback.active_host or not isinstance(snap, dict):
            return
        if "tracks" not in snap:
            if snap.get("error"):
                self.toast(str(snap["error"]))
            return
        from harmony.web.api import track_from_dict

        self._remote_has_queue = bool(snap["tracks"])
        if not snap["tracks"] and self.queue.tracks and not snap.get("playing"):
            # The device has no queue (e.g. this app just restarted) but we do:
            # keep ours on screen; Play loads it onto the device.
            pb = self.playback
            pb.volume = snap.get("volume")
            pb.volume_supported = pb.volume is not None
            return
        keys = [(d.get("service"), str(d.get("id"))) for d in snap["tracks"]]
        mine = [(t.service.value, t.id) for t in self.queue.tracks]
        if keys != mine:
            # Reuse the Track objects we already hold where the key matches, so
            # rows (and their indicators) don't churn on every poll.
            pool: dict[tuple[str, str], list[Any]] = {}
            for t in self.queue.tracks:
                pool.setdefault((t.service.value, t.id), []).append(t)
            rebuilt = []
            for k, d in zip(keys, snap["tracks"], strict=True):
                have = pool.get(k)
                rebuilt.append(have.pop(0) if have else track_from_dict(d))
            self.queue.tracks = rebuilt
            self.queue.original = list(rebuilt)
        self.queue.index = int(snap.get("index", -1))
        self.queue.shuffle = bool(snap.get("shuffle"))
        self.queue.repeat = snap.get("repeat") or "off"
        pb = self.playback
        pb.shuffle, pb.repeat = self.queue.shuffle, self.queue.repeat
        cur = self.queue.current()
        if cur is not None:
            pb.track = cur
            self._now_playing[target] = (cur.title, cur.artist_name)
        state = (snap.get("state") or "").lower()
        if snap.get("playing"):
            pb.state = "paused" if state == "paused" else "playing"
        elif pb.state != "loading" or state in ("stopped", "idle"):
            pb.state = "paused" if state == "paused" else "stopped"
        pos = snap.get("position_s")
        if pos is not None and not (time.monotonic() < self._seek_settle_until
                                    and abs(pos - self._seek_target_s) > 5):
            self._seek_settle_until = 0.0
            pb.position_s = int(pos)
        if snap.get("duration_s"):
            pb.duration_s = int(snap["duration_s"])
        pb.volume = snap.get("volume")
        pb.volume_supported = pb.volume is not None
        err = snap.get("error")
        if err and err != self._last_remote_error:
            self.toast(f"Device playback: {err}")
        self._last_remote_error = err
        self._refresh_nav()
        self.emit("playback-changed")

    # -- the poller (progress for every output; mirrors device queues) -----------

    def _ensure_poller(self) -> None:
        if self._poll_id is None:
            self._poll_id = GLib.timeout_add(1000, self._poll)

    def _poll(self) -> bool:
        target = self.playback.active_host
        if not target:
            self._poll_id = None
            return GLib.SOURCE_REMOVE
        if target == LOCAL_HOST:
            if self._local_player is not None and self.playback.state in ("playing", "paused"):
                self._sync_status_to_playback(target, self._local_player.status())
            return GLib.SOURCE_CONTINUE
        self._poll_tick += 1
        if self._poll_tick % 2 or self._poll_inflight:
            return GLib.SOURCE_CONTINUE  # devices every ~2s, one request at a time
        self._poll_inflight = True
        host, via = self.split_target(target)
        eng = self._engine()

        def done(snap: dict[str, Any]) -> None:
            self._poll_inflight = False
            self._apply_snapshot(target, snap)

        def failed(_exc: BaseException) -> None:
            self._poll_inflight = False  # transient; the next tick retries

        run_async(lambda: eng.device_queue(host, via=via), done, failed)
        return GLib.SOURCE_CONTINUE

    # -- app-wide playback model (Now Playing bar / indicators) -------------

    def _emit_playback(self) -> None:
        """Emit ``playback-changed`` on the main loop (safe from any thread)."""
        on_main(self.emit, "playback-changed")

    def _refresh_nav(self) -> None:
        self.playback.has_prev = self.queue.has_previous() or self.queue.current() is not None
        self.playback.has_next = self.queue.has_next()

    def _mark_now_playing(self, host: str, track: Any, state: str = "playing") -> None:
        """Record ``track`` as now playing on ``host`` and update the model (main loop)."""
        if track is None:
            return
        self._now_playing[host] = (track.title, track.artist_name)
        pb = self.playback
        pb.active_host = host
        pb.track = track
        pb.collection_key = self._active_collection
        pb.state = state
        pb.position_s = 0
        pb.duration_s = getattr(track, "duration_s", None)
        self._refresh_nav()
        self.emit("playback-changed")

    def _sync_status_to_playback(self, host: str, status: Any) -> None:
        """Fold a local-player status read into the model (main loop)."""
        if self.playback.active_host != host:
            return
        pb = self.playback
        if pb.state != "loading":
            pb.state = status.state or pb.state
        if status.position_s is not None:
            # A read that predates a just-issued seek still reports the old
            # position; hold the optimistic one until it converges.
            if not (time.monotonic() < self._seek_settle_until
                    and abs(status.position_s - self._seek_target_s) > 5):
                self._seek_settle_until = 0.0
                pb.position_s = status.position_s
        if status.duration_s is not None:
            pb.duration_s = status.duration_s
        pb.volume = status.volume
        pb.volume_supported = status.volume is not None
        self._refresh_nav()
        self.emit("playback-changed")

    def _stop_local_player(self) -> None:
        """Stop the GStreamer local player if it exists (main loop)."""
        if self._local_player is not None:
            self._local_player.stop()

    def _end_playback(self) -> None:
        """The queue ran out (or was stopped). The queue and the last track stay
        visible so Play starts it again; only the transport resets."""
        if self.playback.active_host == LOCAL_HOST:
            self._stop_local_player()
        self.playback.state = "stopped"
        self.playback.position_s = 0
        self._refresh_nav()
        self.emit("playback-changed")

    def last_played_on(self, host: str | None) -> tuple[str, str] | None:
        """Return the (title, artist) last played on ``host``, if any."""
        if host is None:
            return None
        return self._now_playing.get(host)

    # -- transport (called from the Now Playing bar/page, main loop) ----------

    def _toast_playback_error(self, exc: BaseException, fallback: str) -> None:
        """Toast a short human sentence for a playback failure.

        Reserves the raw exception text for provider-raised errors, which are
        already written for people; anything else gets ``fallback`` and the
        detail goes to the log (mirrors devices_page's ``_report_error``).
        """
        from harmony.errors import NotSupportedError, ProviderError

        if isinstance(exc, (ProviderError, NotSupportedError)):
            self.toast(str(exc))
        else:
            log.warning("playback error: %s (%s)", fallback, exc)
            self.toast(fallback)

    def playback_toggle_pause(self) -> None:
        """Pause if playing; resume if paused; replay the current track if stopped."""
        host = self.playback.active_host
        if not host or self.queue.current() is None:
            return
        pb = self.playback
        if pb.state in ("stopped", "unknown"):
            resume = self._resume_at
            self._resume_at = 0
            self.playback_jump(max(self.queue.index, 0))
            if resume > 5:
                if host == LOCAL_HOST:
                    GLib.timeout_add(1500, lambda: (self._get_local_player().seek(resume), False)[1])
                else:
                    GLib.timeout_add(3000, lambda: (self._remote_control("seek", resume), False)[1])
            return
        pausing = pb.state in ("playing", "loading")
        pb.state = "paused" if pausing else "playing"
        self.emit("playback-changed")
        if host == LOCAL_HOST:
            player = self._get_local_player()
            (player.pause if pausing else player.resume)()
            return
        self._remote_control("pause" if pausing else "resume")

    def playback_stop(self) -> None:
        """Stop (not pause). The queue stays, so Play picks it up again."""
        host = self.playback.active_host
        if not host:
            return
        if host == LOCAL_HOST:
            self._play_gen += 1
            self._end_playback()
            return
        self.playback.state = "stopped"
        self.emit("playback-changed")
        self._remote_op("stop", error="Couldn't stop playback.")

    def playback_next(self) -> None:
        if self._is_remote():
            self._remote_op("next", error="Couldn't skip to the next track.")
            return
        nxt = self.queue.advance(manual=True)
        if nxt is None:
            self._end_playback()
        else:
            self._start_local(nxt)

    def playback_previous(self) -> None:
        """Back a track — or restart this one if it's more than 3s in."""
        if self._is_remote():
            self._remote_op("prev", error="Couldn't go back.")
            return
        idx = self.queue.previous(self.playback.position_s)
        if idx is None:
            return
        if idx == self.queue.index and (self.playback.position_s or 0) > 3 \
                and self.playback.state in ("playing", "paused") and self._local_player is not None:
            self.playback_seek(0)
            return
        self._start_local(idx)

    def playback_jump(self, index: int) -> None:
        """Play the queue item at ``index`` (Now Playing double-click)."""
        if not 0 <= index < len(self.queue.tracks):
            return
        if self._is_remote():
            if self._remote_has_queue:
                self._remote_op("jump", {"index": index}, error="Couldn't play that track.")
            else:  # the device's queue is empty (restart, or it was lost): load ours
                self._remote_op("load", {
                    "tracks": [self._track_dict(t) for t in self.queue.tracks], "start": index,
                    "shuffle": self.playback.shuffle, "repeat": self.playback.repeat,
                    "keep_order": True}, error="Couldn't play that track.")
            return
        self.playback.active_host = LOCAL_HOST
        self._ensure_poller()
        self._start_local(index)

    def playback_play_from(self, track: Any) -> None:
        """Jump to ``track`` in the active queue (by identity, then by key)."""
        idx = next((i for i, t in enumerate(self.queue.tracks) if t is track), None)
        if idx is None:
            key = getattr(track, "key", lambda: None)()
            idx = next((i for i, t in enumerate(self.queue.tracks) if t.key() == key), None)
        if idx is None:
            self.play_list([track])
        else:
            self.playback_jump(idx)

    # -- queue management ----------------------------------------------------

    def playback_enqueue(self, tracks: list[Any]) -> None:
        """Append to the queue; if nothing is playing, start the added tracks."""
        tracks = [t for t in tracks if t is not None]
        if not tracks:
            return
        if not self.playback.active_host:
            self.playback.active_host = LOCAL_HOST
            self._ensure_poller()
        idle = self.playback.state not in ("playing", "paused", "loading")
        if self._is_remote():
            self._remote_op("enqueue", {"tracks": [self._track_dict(t) for t in tracks]})
        else:
            start = self.queue.enqueue(tracks, idle=idle)
            if start is not None:
                self._start_local(start)
            self._refresh_nav()
            self.emit("playback-changed")
        if not idle:
            self.toast(f"Added {len(tracks)} to the queue" if len(tracks) > 1 else "Added to the queue")

    def playback_play_next(self, tracks: list[Any]) -> None:
        """Insert right after the current track (start them if idle)."""
        tracks = [t for t in tracks if t is not None]
        if not tracks:
            return
        if not self.playback.active_host:
            self.playback.active_host = LOCAL_HOST
            self._ensure_poller()
        idle = self.playback.state not in ("playing", "paused", "loading")
        if self._is_remote():
            self._remote_op("play_next", {"tracks": [self._track_dict(t) for t in tracks]})
        else:
            start = self.queue.play_next(tracks, idle=idle)
            if start is not None:
                self._start_local(start)
            self._refresh_nav()
            self.emit("playback-changed")
        if not idle:
            self.toast("Playing next")

    def playback_reorder(self, from_index: int, to_index: int) -> None:
        """Move a queue item (the current track keeps playing wherever it lands)."""
        if self._is_remote():
            self._remote_op("move", {"from": from_index, "to": to_index})
            return
        self.queue.move(from_index, to_index)
        self._refresh_nav()
        self.emit("playback-changed")

    def playback_remove_at(self, index: int) -> None:
        if self._is_remote():
            self._remote_op("remove", {"index": index})
            return
        removed_current, start = self.queue.remove(index)
        if removed_current:
            if start is not None and self.playback.state in ("playing", "loading"):
                self._start_local(start)
            elif start is None:
                self._end_playback()
        self._refresh_nav()
        self.emit("playback-changed")

    def playback_remove(self, track: Any) -> None:
        idx = next((i for i, t in enumerate(self.queue.tracks) if t is track), None)
        if idx is not None:
            self.playback_remove_at(idx)

    def playback_clear(self) -> None:
        """Clear the queue except what's playing."""
        if self._is_remote():
            self._remote_op("clear")
            return
        self.queue.clear()
        self._refresh_nav()
        self.emit("playback-changed")

    def playback_seek(self, position_s: int) -> None:
        """Seek the active output to ``position_s``."""
        host = self.playback.active_host
        if not host:
            return
        self.playback.position_s = int(position_s)
        self._seek_target_s = int(position_s)
        self._seek_settle_until = time.monotonic() + 4.0  # ride out one stale poll
        self.emit("playback-changed")
        if host == LOCAL_HOST:
            if not self._get_local_player().seek(int(position_s)):
                self._seek_settle_until = 0.0
                self.toast("This track doesn't support seeking.")
            return
        self._remote_control("seek", int(position_s), error="Couldn't seek in this track.")

    def playback_set_volume(self, level: int) -> None:
        """Set the active output's volume (0..100)."""
        host = self.playback.active_host
        if not host:
            return
        level = max(0, min(100, int(level)))
        self.playback.volume = level
        if host == LOCAL_HOST:
            self._get_local_player().set_volume(level)
            return
        self._remote_control("volume", level, error="Couldn't change the volume.")

    def playback_set_active_device(self, target: str) -> None:
        """Move the whole active queue — order, current track and position — to
        ``target``. One active stream per instance; the old output stops."""
        old = self.playback.active_host
        if not target or old == target:
            return
        tracks, index = list(self.queue.tracks), self.queue.index
        position = int(self.playback.position_s or 0)
        was_playing = self.playback.state in ("playing", "loading")
        if old:
            self._halt_output(old)
        self.playback.active_host = target
        self._ensure_poller()
        if not tracks or index < 0:
            self.queue = self._fresh_queue()
            self.playback.track = None
            self.playback.state = "stopped"
            self.emit("playback-changed")
            return
        if not was_playing:
            # Paused/stopped: carry the queue over without starting it.
            if target == LOCAL_HOST:
                self.playback.state = "stopped"
                self.emit("playback-changed")
                return
        if target == LOCAL_HOST:
            self._start_local(index)
            if position > 5:
                GLib.timeout_add(1500, lambda: (self._get_local_player().seek(position), False)[1])
            return
        self._mark_now_playing(target, tracks[index], state="loading")
        host, via = self.split_target(target)
        eng = self._engine()
        body = {"tracks": [self._track_dict(t) for t in tracks], "start": index,
                "shuffle": self.playback.shuffle, "repeat": self.playback.repeat,
                "keep_order": True}

        def work() -> dict[str, Any]:
            snap = eng.device_queue_op(host, "load", body, via=via)
            if position > 5:
                time.sleep(3)  # let the device start before seeking into the track
                eng.device_control(host, "seek", position, via=via)
            return snap

        run_async(work, lambda snap: self._apply_snapshot(target, snap),
                  lambda exc: self._toast_playback_error(exc, "Couldn't move playback to that device."))

    def playback_set_shuffle(self, on: bool) -> None:
        self.playback.shuffle = bool(on)
        if self._is_remote():
            self._remote_op("shuffle", {"on": bool(on)})
        else:
            self.queue.set_shuffle(bool(on))
            self._refresh_nav()
        self.emit("playback-changed")

    def playback_set_repeat(self, mode: str) -> None:
        """Set repeat mode: ``"off" | "all" | "one"``."""
        if mode not in ("off", "all", "one"):
            return
        self.playback.repeat = mode
        if self._is_remote():
            self._remote_op("repeat", {"mode": mode})
        else:
            self.queue.set_repeat(mode)
            self._refresh_nav()
        self.emit("playback-changed")

    # -- peers' devices ("via") --------------------------------------------------

    def discover_outputs(self) -> bool:
        """Find LAN renderers + mesh peers' renderers in the background so the
        output picker is populated without visiting the Devices page."""
        eng = self._engine()

        def work() -> list[Any]:
            from harmony.playback import DeviceInfo

            known = {e.get("host") for e in self.settings.known_devices}
            return [DeviceInfo(id=d["host"], name=d.get("name") or d["host"], host=d["host"],
                               kind=d.get("kind", "wiim"))
                    for d in eng.devices().get("devices", [])
                    if d.get("host") and d["host"] not in known]

        def done(found: list[Any]) -> None:
            if found and not getattr(self, "_discovered", None):
                self.set_discovered_devices(found)

        run_async(work, done, lambda exc: log.debug("device discovery failed: %s", exc))
        self.refresh_peer_devices()
        return GLib.SOURCE_REMOVE

    # -- queue survives a restart --------------------------------------------

    @staticmethod
    def _queue_file() -> Any:
        from harmony.config import data_dir

        return data_dir() / "play-queue.json"

    def _schedule_queue_save(self) -> None:
        if self._save_id is None:
            self._save_id = GLib.timeout_add_seconds(2, self._save_queue)

    def _save_queue(self) -> bool:
        self._save_id = None
        import json

        pb = self.playback
        data = {
            "target": pb.active_host, "index": self.queue.index,
            "position_s": pb.position_s or 0, "shuffle": pb.shuffle, "repeat": pb.repeat,
            "collection": [self._active_collection[0].value, self._active_collection[1]]
            if self._active_collection else None,
            "tracks": [self._track_dict(t) for t in self.queue.tracks],
        }
        try:
            path = self._queue_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), "utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.debug("couldn't save the queue: %s", exc)
        return GLib.SOURCE_REMOVE

    def restore_queue(self) -> None:
        """Bring back the last session's queue, paused on the track it was on."""
        import json

        from harmony.web.api import track_from_dict

        try:
            data = json.loads(self._queue_file().read_text("utf-8"))
            tracks = [track_from_dict(d) for d in data.get("tracks") or []]
        except (OSError, ValueError, KeyError, TypeError):
            return
        if not tracks:
            return
        pb = self.playback
        pb.shuffle, pb.repeat = bool(data.get("shuffle")), data.get("repeat") or "off"
        self.queue = self._fresh_queue()
        self.queue.tracks, self.queue.original = tracks, list(tracks)
        self.queue.index = max(0, min(int(data.get("index") or 0), len(tracks) - 1))
        coll = data.get("collection")
        try:
            self._active_collection = (Service(coll[0]), str(coll[1])) if coll else None
        except (ValueError, IndexError):
            self._active_collection = None
        pb.active_host = data.get("target") or LOCAL_HOST
        pb.track = self.queue.current()
        pb.collection_key = self._active_collection
        pb.state = "stopped"
        pb.position_s = int(data.get("position_s") or 0)
        pb.duration_s = getattr(pb.track, "duration_s", None)
        self._resume_at = pb.position_s
        self._refresh_nav()
        self._ensure_poller()
        self.emit("playback-changed")

    def refresh_peer_devices(self) -> None:
        """Fetch mesh peers' renderers (worker) so they show in the output picker."""
        eng = self._engine()

        def work() -> list[Any]:
            from harmony.playback import DeviceInfo

            out = []
            for d in eng.federated_devices().get("devices", []):
                via = d.get("via")
                if not via or not d.get("host"):
                    continue
                target = f"{via}/{d['host']}"
                out.append(DeviceInfo(id=target, name=f"{d.get('name') or d['host']} "
                                      f"(via {d.get('via_name') or via})",
                                      host=target, kind=d.get("kind", "wiim")))
            return out

        def done(devs: list[Any]) -> None:
            self._peer_devices = devs
            self.emit("devices-changed")

        run_async(work, done, lambda exc: log.debug("peer devices unavailable: %s", exc))

    def toast(self, text: str) -> None:
        """Emit a toast. Must be called from the main thread."""
        self.emit("toast", text)
