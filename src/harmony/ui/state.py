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
import random
import time
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, GObject  # noqa: E402

from harmony.config import CredentialStore, Settings  # noqa: E402
from harmony.models import Playlist, Service  # noqa: E402
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
        return self.track is not None and self.state in ("playing", "paused")

# Synthetic host id for the in-app local player ("This computer"). Routed to a
# GStreamer LocalPlayer instead of the relay + a network device.
LOCAL_HOST = "__local__"

# How often the queue poller checks a device for a track ending, in seconds.
_QUEUE_POLL_S = 3
# How close (seconds) the reported position must get to the track's duration to
# count the track as finished. A couple of seconds absorbs the poll interval and
# the device rounding/settling its final position.
_END_EPSILON_S = 3

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

        # Lazily created + started on the first play-to-device request; a
        # single relay serves every device for the app's lifetime (a daemon
        # thread, so it goes away with the process).
        self._relay: Any | None = None
        # host -> (title, artist) we last relayed to that device, so the UI can
        # show now-playing text even when the device reports none (a bare URL
        # carries no metadata unless the stream itself does).
        self._now_playing: dict[str, tuple[str, str]] = {}
        # host -> UpnpRenderer|None, probed once per device (None = no
        # AVTransport, use the httpapi path instead of re-probing every play).
        self._upnp_cache: dict[str, Any] = {}
        # Play-to-device queues (for playing an album/artist/playlist as a
        # sequence). host -> remaining tracks; a per-host main-loop poller
        # advances to the next track when the current one ends.
        self._queues: dict[str, list[Any]] = {}
        self._queue_prev_state: dict[str, str] = {}
        self._queue_poll_ids: dict[str, int] = {}
        # Per-host: True once we've seen the current track playing mid-way, so a
        # single near-end reading advances exactly once (not every poll near the
        # end). Re-armed when the next track is seen mid-play.
        self._queue_armed: dict[str, bool] = {}
        # Per-host play context for the media-player UI: the full track order a
        # queue was built from (for repeat + shuffle), which collection it came
        # from, and the tracks already played (so "previous" can go back).
        self._collection_full: dict[str, list[Any]] = {}
        self._collection_key: dict[str, tuple[Service, str] | None] = {}
        self._history: dict[str, list[Any]] = {}
        # The one app-wide playback model the Now Playing bar reflects/controls.
        self.playback = PlaybackState()
        # After a seek, hold the optimistic position until the device/player
        # actually converges: an in-flight status poll started before the seek
        # would otherwise write the pre-seek position back and snap the bar (and
        # the perceived play head) back to where it was.
        self._seek_settle_until = 0.0
        self._seek_target_s = 0
        # The in-app GStreamer player ("This computer"); created on first use.
        self._local_player: Any | None = None

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
        return [local, *self.all_devices()]

    def _get_local_player(self) -> Any:
        """Lazily create the GStreamer local player (main loop only)."""
        if self._local_player is None:
            from harmony.ui.local_player import LocalPlayer

            self._local_player = LocalPlayer(
                on_eos=self._on_local_eos,
                on_error=lambda msg: self.toast(f"Local playback error: {msg}"),
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

    def _on_local_eos(self) -> None:
        """A locally-played track ended: advance the queue or stop (main loop)."""
        host = LOCAL_HOST
        if self.playback.active_host != host:
            return
        nxt = self._queue_step_forward(host, allow_repeat_one=True)
        if nxt is not None:
            run_async(lambda: self._play_one(nxt, host), None,
                      lambda exc: log.warning("Local queue advance failed: %s", exc))
        else:
            self._end_playback(host)

    def _queue_step_forward(self, host: str, *, allow_repeat_one: bool) -> Any | None:
        """Pop the current track and return the next to play, honouring repeat/
        shuffle; ``None`` means playback should end. Pure (in-memory only)."""
        if allow_repeat_one and self.playback.repeat == "one" and self.playback.track is not None:
            return self.playback.track
        queue = self._queues.get(host)
        if not queue:
            return None
        if self.playback.track is not None:
            self._history.setdefault(host, []).append(self.playback.track)
        queue.pop(0)
        if queue:
            return queue[0]
        if self.playback.repeat == "all" and self._collection_full.get(host):
            refilled = list(self._collection_full[host])
            if self.playback.shuffle:
                random.shuffle(refilled)
            self._queues[host] = refilled
            return refilled[0]
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

    def _get_relay(self) -> Any:
        """Lazily create and start the shared playback relay (engine layer).

        Imported lazily like ``device_for`` so a headless/no-GTK import of this
        module never pays for ``harmony.playback``. The relay binds an OS-chosen
        port on all interfaces and serves on a daemon thread, so it costs
        nothing until the first play-to-device request and needs no explicit
        shutdown.
        """
        if self._relay is None:
            from harmony.playback import RelayServer

            relay = RelayServer()
            relay.start()
            self._relay = relay
        return self._relay

    def play_track_on_device(self, track: Any, device_host: str) -> None:
        """Play a single track on a device, superseding any active queue for it.

        Blocking — MUST run on a worker thread via ``run_async``. A single-track
        play cancels an in-progress album/playlist queue on the same device (the
        queue-teardown is marshalled to the main loop, where its poller lives).
        """
        self._stop_other_devices(device_host)  # one active stream per instance
        on_main(self._stop_queue, device_host)
        if device_host != LOCAL_HOST:
            on_main(self._stop_local_player)
        self._collection_key[device_host] = None
        self._history[device_host] = []
        self.playback.active_host = device_host
        self._play_one(track, device_host)
        on_main(self._start_queue_poller, device_host)

    def play_tracks_on_device(
        self,
        tracks: list[Any],
        device_host: str,
        collection_key: tuple[Service, str] | None = None,
    ) -> None:
        """Play a sequence of tracks (an album/artist/playlist) on a device.

        Blocking — MUST run on a worker thread (it plays the first track). The
        rest are advanced by a main-loop poller that watches for each track to
        finish. Replaces any existing queue on that device; an empty list is a
        no-op. ``collection_key`` is the album/playlist's ``(service, id)`` so
        on-screen indicators can light up the source collection.
        """
        tracks = list(tracks)
        if not tracks:
            return
        self._stop_other_devices(device_host)  # one active stream per instance
        if device_host != LOCAL_HOST:
            on_main(self._stop_local_player)
        order = list(tracks)
        if self.playback.shuffle:
            random.shuffle(order)
        self._collection_full[device_host] = list(tracks)
        self._collection_key[device_host] = collection_key
        self._history[device_host] = []
        self._queues[device_host] = order
        self._queue_prev_state[device_host] = ""
        self.playback.active_host = device_host
        self._play_one(order[0], device_host)
        on_main(self._start_queue_poller, device_host)

    def _start_queue_poller(self, host: str) -> None:
        if host not in self._queue_poll_ids:
            self._queue_poll_ids[host] = GLib.timeout_add_seconds(_QUEUE_POLL_S, self._poll_queue, host)

    def _stop_queue(self, host: str) -> None:
        """Forget a device's queue and stop its poller (main loop only)."""
        self._queues.pop(host, None)
        self._queue_prev_state.pop(host, None)
        self._queue_armed.pop(host, None)
        source_id = self._queue_poll_ids.pop(host, None)
        if source_id is not None:
            GLib.source_remove(source_id)

    def _next_after_status(
        self, host: str, state: str, position: int | None, duration: int | None
    ) -> Any:
        """Advance the queue if the current track just finished; return the next track or None.

        Primary signal is progress: the reported position reaching the track's
        duration — device-agnostic, and driven by the same data the progress bar
        reads. Falls back to a state edge (``playing`` -> ``stopped``) only when
        the device reports no duration. Armed by mid-track playback so one
        near-end reading advances exactly once. Pure: only touches the in-memory
        queue dicts, no I/O.
        """
        prev = self._queue_prev_state.get(host, "")
        self._queue_prev_state[host] = state or ""
        queue = self._queues.get(host)
        if not queue:
            return None

        has_duration = bool(duration and duration > 0 and position is not None)
        near_end = has_duration and position >= duration - _END_EPSILON_S
        mid_track = has_duration and position < duration - _END_EPSILON_S
        if mid_track:
            self._queue_armed[host] = True  # a real track is under way; arm end-detection

        ended = False
        if near_end and self._queue_armed.get(host):
            self._queue_armed[host] = False  # advance once, until the next track is mid-play
            ended = True
        elif not has_duration and prev == "playing" and state == "stopped":
            ended = True  # no progress info -> fall back to the state edge

        if ended:
            repeat = self.playback.repeat
            if repeat == "one":
                return queue[0]  # replay the current track, don't advance
            finished = queue.pop(0)
            self._history.setdefault(host, []).append(finished)
            if queue:
                return queue[0]
            if repeat == "all" and self._collection_full.get(host):
                refilled = list(self._collection_full[host])
                if self.playback.shuffle:
                    random.shuffle(refilled)
                self._queues[host] = refilled
                return refilled[0]
            self._stop_queue(host)
        return None

    def _poll_queue(self, host: str) -> bool:
        # Poll while this host has a queue OR is the active single-track
        # playback (so the Now Playing seek bar advances for single tracks too).
        active = self.playback.active_host == host
        if host not in self._queues and not active:
            self._queue_poll_ids.pop(host, None)
            return GLib.SOURCE_REMOVE
        if host == LOCAL_HOST:
            # Local playback has no network device: read the GStreamer player's
            # status directly (fast, main loop). Track-end is driven by EOS
            # (``_on_local_eos``), not position.
            if self._local_player is not None:
                self._sync_status_to_playback(host, self._local_player.status())
            return GLib.SOURCE_CONTINUE
        device = self.device_for(host)

        def done(status: Any) -> None:
            self._sync_status_to_playback(host, status)
            if host in self._queues:
                next_track = self._next_after_status(
                    host, status.state or "", status.position_s, status.duration_s
                )
                if next_track is not None:
                    if self.playback.track is not None and next_track is not self.playback.track:
                        self._history.setdefault(host, []).append(self.playback.track)
                    run_async(
                        lambda: self._play_one(next_track, host),
                        None,
                        lambda exc: log.warning("Queue advance on %s failed: %s", host, exc),
                    )
            elif active and (status.state or "") == "stopped":
                # A single track finished with nothing queued behind it.
                self._end_playback(host)

        run_async(device.status, done, lambda _exc: None)  # transient poll errors ignored
        return GLib.SOURCE_CONTINUE

    # -- app-wide playback model (Now Playing bar / indicators) -------------

    def _emit_playback(self) -> None:
        """Emit ``playback-changed`` on the main loop (safe from any thread)."""
        on_main(self.emit, "playback-changed")

    def _mark_now_playing(self, host: str, track: Any) -> None:
        """Record ``track`` as now playing on ``host`` and update the model.

        Runs on the worker thread that played the track; only touches in-memory
        state and marshals the signal to the main loop.
        """
        self._now_playing[host] = (track.title, track.artist_name)
        pb = self.playback
        pb.active_host = host
        pb.track = track
        pb.collection_key = self._collection_key.get(host)
        pb.state = "playing"
        pb.position_s = 0
        pb.duration_s = getattr(track, "duration_s", None)
        pb.has_prev = bool(self._history.get(host))
        pb.has_next = bool(self._queues.get(host)) or pb.repeat != "off"
        self._emit_playback()

    def _sync_status_to_playback(self, host: str, status: Any) -> None:
        """Fold a device status poll into the model (main loop; active host only)."""
        if self.playback.active_host != host:
            return
        pb = self.playback
        pb.state = status.state or pb.state
        if status.position_s is not None:
            # A poll that predates a just-issued seek still reports the old
            # position; ignore it until the reported position converges on the
            # seek target (or the settle window lapses), so the bar doesn't snap
            # back to where the user seeked away from.
            if (
                time.monotonic() < self._seek_settle_until
                and abs(status.position_s - self._seek_target_s) > 5
            ):
                pass
            else:
                self._seek_settle_until = 0.0
                pb.position_s = status.position_s
        if status.duration_s is not None:
            pb.duration_s = status.duration_s
        pb.volume = status.volume
        pb.volume_supported = status.volume is not None
        pb.has_prev = bool(self._history.get(host))
        pb.has_next = bool(self._queues.get(host)) or pb.repeat != "off"
        self.emit("playback-changed")

    def _stop_local_player(self) -> None:
        """Stop the GStreamer local player if it exists (main loop)."""
        if self._local_player is not None:
            self._local_player.stop()

    def _end_playback(self, host: str) -> None:
        """The active playback stopped with nothing left: reset the model."""
        if host == LOCAL_HOST:
            self._stop_local_player()
        self._stop_queue(host)
        if self.playback.active_host == host:
            self.playback.state = "stopped"
            self.playback.track = None
            self.playback.collection_key = None
            self.playback.has_prev = False
            self.playback.has_next = False
            self.emit("playback-changed")

    def _active_device(self) -> Any | None:
        host = self.playback.active_host
        return self.device_for(host) if host else None

    def _teardown_device(self, host: str) -> None:
        """Stop playback on ``host`` and forget its queue — used when the single
        active stream moves or is superseded. Runs on a worker thread; queue
        teardown is marshalled to the main loop where its poller lives."""
        if not host:
            return
        on_main(self._stop_queue, host)
        self._now_playing.pop(host, None)
        self._collection_key.pop(host, None)
        self._collection_full.pop(host, None)
        self._history.pop(host, None)
        if host == LOCAL_HOST:
            on_main(self._stop_local_player)
        else:
            try:
                self.device_for(host).stop()
            except Exception:  # noqa: BLE001 - best-effort stop; the move continues
                log.debug("stop on %s during handoff failed", host, exc_info=True)

    def _stop_other_devices(self, keep: str) -> None:
        """One active stream per instance: stop the previously-active device when
        a new stream starts elsewhere. Worker thread."""
        old = self.playback.active_host
        if old and old != keep:
            self._teardown_device(old)

    def _play_one(self, track: Any, device_host: str) -> None:
        """Resolve ``track``'s stream, register it with the relay, and play it (queue-agnostic).

        The stream is resolved once up front so auth/subscription/codec failures
        surface immediately (before a URL reaches the device), then wrapped in a
        resolver the relay re-invokes per fetch, re-resolving only once the
        provider's time-limited URL is old enough to have expired.
        """
        provider = self.providers.get(track.service)
        if provider is None:
            raise RuntimeError(f"No provider configured for {track.service.label}")

        # The in-app player decodes locally, so ask for the highest tier the
        # track allows; casting keeps the LAN-compatible default so every
        # renderer can decode what the relay forwards.
        want_max = device_host == LOCAL_HOST
        cached = {"source": provider.resolve_stream(track.id, max_quality=want_max), "at": time.monotonic()}
        ttl_s = 600.0

        # "This computer": decode + play the resolved stream locally via
        # GStreamer, no relay/device. GStreamer must be driven on the main loop.
        if device_host == LOCAL_HOST:
            source = cached["source"]
            log.info("Local playback: %s (%s)", getattr(source, "label", "?"), source.mime_type)
            on_main(lambda: self._get_local_player().load_and_play(source.url, dict(source.headers)))
            self._mark_now_playing(device_host, track)
            return

        def resolver() -> Any:
            if time.monotonic() - cached["at"] > ttl_s:
                cached["source"] = provider.resolve_stream(track.id, max_quality=want_max)
                cached["at"] = time.monotonic()
            return cached["source"]

        relay = self._get_relay()
        source = cached["source"]

        # Chromecast: it isn't a UPnP renderer, so hand the relay URL to its media
        # receiver directly with the track metadata (its on-screen card shows it).
        # Passthrough relay (allow_icy=False) — Cast reads metadata from play_media.
        if self._device_entry(device_host).get("kind") == "cast":
            token = relay.register(resolver, title=track.title, artist=track.artist_name, allow_icy=False)
            url = relay.url_for(token, device_host)
            self.device_for(device_host).play_url(
                url,
                title=track.title,
                artist=track.artist_name,
                album=getattr(track, "album", None),
                art_url=getattr(track, "artwork_url", None),
                duration_s=getattr(track, "duration_s", None),
                mime=source.mime_type or "audio/mpeg",
            )
            self._mark_now_playing(device_host, track)
            return

        # Prefer UPnP AVTransport: DIDL-Lite carries title/artist/album/art +
        # duration (so the device's own screen shows the track and reports a
        # progress/duration), and it plays a plain seekable file — so register
        # the relay in passthrough mode (allow_icy=False). Fall back to the
        # LinkPlay httpapi (+ best-effort ICY metadata) if there's no AVTransport.
        renderer = self._upnp_renderer_for(device_host)
        if renderer is not None:
            token = relay.register(resolver, title=track.title, artist=track.artist_name, allow_icy=False)
            url = relay.url_for(token, device_host)
            try:
                renderer.play_media(
                    url,
                    title=track.title,
                    artist=track.artist_name,
                    album=getattr(track, "album", None),
                    art_url=getattr(track, "artwork_url", None),
                    duration_s=getattr(track, "duration_s", None),
                    mime=source.mime_type or "audio/mpeg",
                )
                self._mark_now_playing(device_host, track)
                return
            except Exception as exc:  # noqa: BLE001 - any UPnP failure -> httpapi fallback
                log.warning("UPnP play to %s failed (%s); falling back to httpapi", device_host, exc)

        token = relay.register(resolver, title=track.title, artist=track.artist_name)
        url = relay.url_for(token, device_host)
        self.device_for(device_host).play_url(url)
        self._mark_now_playing(device_host, track)

    def _upnp_renderer_for(self, host: str) -> Any:
        """Return a cached ``UpnpRenderer`` for ``host``, or None if it has no AVTransport.

        Probes once per host (SSDP + a description fetch, both on the caller's
        worker thread) and caches the result — including a None for a device that
        turned out not to speak UPnP, so an httpapi-only device isn't re-probed
        on every play.
        """
        if host in self._upnp_cache:
            return self._upnp_cache[host]
        renderer = None
        try:
            from harmony.playback import upnp

            if self._device_session is None:
                import requests

                self._device_session = requests.Session()
            description = upnp.description_url_for(host)
            service = upnp.find_avtransport(description, self._device_session) if description else None
            if service is not None:
                renderer = upnp.UpnpRenderer(service, session=self._device_session)
        except Exception as exc:  # noqa: BLE001 - UPnP is optional; degrade to httpapi
            log.debug("UPnP probe failed for %s: %s", host, exc)
        self._upnp_cache[host] = renderer
        return renderer

    def last_played_on(self, host: str | None) -> tuple[str, str] | None:
        """Return the (title, artist) last relayed to ``host`` via play-to-device, if any."""
        if host is None:
            return None
        return self._now_playing.get(host)

    # -- transport (called from the Now Playing bar, main loop) --------------

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
            log.exception("playback error: %s", fallback)
            self.toast(fallback)

    def playback_toggle_pause(self) -> None:
        """Pause if playing, resume if paused/stopped, on the active device."""
        host = self.playback.active_host
        if not host:
            return
        pausing = self.playback.state == "playing"
        self.playback.state = "paused" if pausing else "playing"
        self.emit("playback-changed")
        if host == LOCAL_HOST:
            player = self._get_local_player()
            (player.pause if pausing else player.resume)()
            return
        device = self.device_for(host)
        action = device.pause if pausing else device.resume
        run_async(action, None, lambda exc: self._toast_playback_error(exc, "Couldn't control playback."))

    def playback_next(self) -> None:
        """Skip to the next queued track (wraps if repeat is on)."""
        host = self.playback.active_host
        if not host or not self._queues.get(host):
            return
        self._queue_armed[host] = False
        nxt = self._queue_step_forward(host, allow_repeat_one=False)
        if nxt is None:
            self._end_playback(host)
            return
        run_async(lambda: self._play_one(nxt, host), None,
                  lambda exc: self._toast_playback_error(exc, "Couldn't skip to the next track."))

    def playback_previous(self) -> None:
        """Go back to the previously played track (no-op if none)."""
        host = self.playback.active_host
        history = self._history.get(host) if host else None
        if not host or not history:
            return
        prev = history.pop()
        self._queues.setdefault(host, []).insert(0, prev)  # prev becomes the current front
        self._queue_armed[host] = False
        run_async(lambda: self._play_one(prev, host), None,
                  lambda exc: self._toast_playback_error(exc, "Couldn't go back to the previous track."))

    def playback_seek(self, position_s: int) -> None:
        """Seek the active device to ``position_s`` (UPnP only; toasts otherwise)."""
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

        def work() -> None:
            renderer = self._upnp_renderer_for(host)
            if renderer is None:
                raise RuntimeError("this device doesn't support seeking")
            renderer.seek(int(position_s))

        run_async(work, None, lambda exc: self._toast_playback_error(exc, "Couldn't seek in this track."))

    def playback_set_volume(self, level: int) -> None:
        """Set the active device's volume (0..100)."""
        host = self.playback.active_host
        if not host:
            return
        level = max(0, min(100, int(level)))
        self.playback.volume = level
        if host == LOCAL_HOST:
            self._get_local_player().set_volume(level)
            return
        device = self.device_for(host)
        run_async(lambda: device.set_volume(level), None,
                  lambda exc: self._toast_playback_error(exc, "Couldn't change the volume."))

    def playback_set_active_device(self, host: str) -> None:
        """Move the single active stream — its whole queue — to ``host``.

        One active stream per instance: choosing a different device in the Now
        Playing bar hands the current queue off to it (stopping the old device
        and resuming from the current track, order preserved), rather than
        starting a second stream. If nothing is playing, just point the bar at
        ``host``.
        """
        old = self.playback.active_host
        if not host or old == host:
            return

        queue = list(self._queues.get(old) or [])
        if not queue and self.playback.track is not None:
            queue = [self.playback.track]  # a single track with no queue behind it
        collection_key = self._collection_key.get(old)
        collection_full = list(self._collection_full.get(old) or queue)

        if not queue:
            # Nothing playing: just switch which device the bar reflects.
            self.playback.active_host = host
            if self._now_playing.get(host) is None:
                self.playback.track = None
                self.playback.state = "stopped"
            self.emit("playback-changed")
            self._start_queue_poller(host)
            return

        def work() -> None:
            self._teardown_device(old)  # stop the old device (single stream)
            if host != LOCAL_HOST:
                on_main(self._stop_local_player)
            # Preserve the active order (current track first) — no reshuffle.
            self._collection_full[host] = collection_full
            self._collection_key[host] = collection_key
            self._history[host] = []
            self._queues[host] = queue
            self._queue_prev_state[host] = ""
            self.playback.active_host = host
            self._play_one(queue[0], host)
            on_main(self._start_queue_poller, host)

        run_async(work, None,
                  lambda exc: self._toast_playback_error(exc, "Couldn't move playback to that device."))

    def playback_set_shuffle(self, on: bool) -> None:
        """Toggle shuffle; reshuffles the remaining queue when turned on."""
        self.playback.shuffle = bool(on)
        host = self.playback.active_host
        if on and host and self._queues.get(host):
            queue = self._queues[host]
            head, rest = queue[0], queue[1:]
            random.shuffle(rest)
            self._queues[host] = [head, *rest]  # keep the current track playing
        self.emit("playback-changed")

    def playback_set_repeat(self, mode: str) -> None:
        """Set repeat mode: ``"off" | "all" | "one"``."""
        if mode not in ("off", "all", "one"):
            return
        self.playback.repeat = mode
        host = self.playback.active_host
        if host:
            self.playback.has_next = bool(self._queues.get(host)) or mode != "off"
        self.emit("playback-changed")

    def toast(self, text: str) -> None:
        """Emit a toast. Must be called from the main thread."""
        self.emit("toast", text)
