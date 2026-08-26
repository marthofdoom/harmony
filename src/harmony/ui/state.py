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
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, GObject  # noqa: E402

from harmony.config import CredentialStore, Settings  # noqa: E402
from harmony.models import Playlist, Service  # noqa: E402
from harmony.tasks import on_main, run_async  # noqa: E402

# How often the queue poller checks a device for a track ending, in seconds.
_QUEUE_POLL_S = 3

log = logging.getLogger(__name__)


class AppState(GObject.Object):
    """Holds backend singletons and notifies pages of changes via signals."""

    __gsignals__ = {
        "providers-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "playlists-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # The optional integrations (AI planner, recommender sources) were
        # reconfigured. Pages that render "not configured" placeholders must
        # listen for this, or those placeholders survive the user fixing the
        # very thing they complain about.
        "integrations-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # The known-devices list (add/remove/rename) changed; devices_page
        # rebuilds its list from ``known_devices()`` in response.
        "devices-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
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
            self.toast(f"Couldn't load playlists: {exc}")
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

    def add_device(self, host: str, name: str | None = None) -> None:
        """Add a device by host, deduped by host. No-op if already known."""
        host = host.strip()
        if not host:
            return
        if any(entry.get("host") == host for entry in self.settings.known_devices):
            return
        self.settings.known_devices.append({"host": host, "name": (name or host).strip() or host, "kind": "wiim"})
        self.settings.save()
        self.emit("devices-changed")

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
        on_main(self._stop_queue, device_host)
        self._play_one(track, device_host)

    def play_tracks_on_device(self, tracks: list[Any], device_host: str) -> None:
        """Play a sequence of tracks (an album/artist/playlist) on a device.

        Blocking — MUST run on a worker thread (it plays the first track). The
        rest are advanced by a main-loop poller that watches for each track to
        finish. Replaces any existing queue on that device; an empty list is a
        no-op.
        """
        tracks = list(tracks)
        if not tracks:
            return
        self._queues[device_host] = tracks
        self._queue_prev_state[device_host] = ""
        self._play_one(tracks[0], device_host)
        on_main(self._start_queue_poller, device_host)

    def _start_queue_poller(self, host: str) -> None:
        if host not in self._queue_poll_ids:
            self._queue_poll_ids[host] = GLib.timeout_add_seconds(_QUEUE_POLL_S, self._poll_queue, host)

    def _stop_queue(self, host: str) -> None:
        """Forget a device's queue and stop its poller (main loop only)."""
        self._queues.pop(host, None)
        self._queue_prev_state.pop(host, None)
        source_id = self._queue_poll_ids.pop(host, None)
        if source_id is not None:
            GLib.source_remove(source_id)

    def _next_after_status(self, host: str, state: str) -> Any:
        """Advance the queue if the current track just ended; return the next track or None.

        "Ended" = the device was ``playing`` and is now ``stopped``. Pure enough
        to unit-test: only touches the in-memory queue dicts, no I/O.
        """
        prev = self._queue_prev_state.get(host, "")
        self._queue_prev_state[host] = state or ""
        queue = self._queues.get(host)
        if not queue:
            return None
        if prev == "playing" and state == "stopped":
            queue.pop(0)
            if queue:
                return queue[0]
            self._stop_queue(host)
        return None

    def _poll_queue(self, host: str) -> bool:
        if host not in self._queues:
            self._queue_poll_ids.pop(host, None)
            return GLib.SOURCE_REMOVE
        device = self.device_for(host)

        def done(status: Any) -> None:
            next_track = self._next_after_status(host, status.state or "")
            if next_track is not None:
                run_async(
                    lambda: self._play_one(next_track, host),
                    None,
                    lambda exc: log.warning("Queue advance on %s failed: %s", host, exc),
                )

        run_async(device.status, done, lambda _exc: None)  # transient poll errors ignored
        return GLib.SOURCE_CONTINUE

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

        cached = {"source": provider.resolve_stream(track.id), "at": time.monotonic()}
        ttl_s = 600.0

        def resolver() -> Any:
            if time.monotonic() - cached["at"] > ttl_s:
                cached["source"] = provider.resolve_stream(track.id)
                cached["at"] = time.monotonic()
            return cached["source"]

        relay = self._get_relay()
        source = cached["source"]

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
                self._now_playing[device_host] = (track.title, track.artist_name)
                return
            except Exception as exc:  # noqa: BLE001 - any UPnP failure -> httpapi fallback
                log.warning("UPnP play to %s failed (%s); falling back to httpapi", device_host, exc)

        token = relay.register(resolver, title=track.title, artist=track.artist_name)
        url = relay.url_for(token, device_host)
        self.device_for(device_host).play_url(url)
        self._now_playing[device_host] = (track.title, track.artist_name)

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

    def toast(self, text: str) -> None:
        """Emit a toast. Must be called from the main thread."""
        self.emit("toast", text)
