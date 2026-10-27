"""Chromecast / Google TV playback backend.

Casts a stream URL to a Chromecast-enabled device (Google TV, Nest/Home
speakers, Chromecast Audio, TVs with Cast built in) via its default media
receiver — the same "hand the renderer a URL" model as the WiiM/UPnP backends,
so it plugs into the relay and ``CastController`` unchanged.

``pychromecast`` is an **optional** dependency (the ``cast`` extra). It isn't
imported at module load, so a build without it still runs — discovery just
returns nothing and constructing a device raises ``NotSupportedError``. GTK-free
(engine layer).

Note on control latency: unlike LinkPlay's stateless HTTP API, Cast uses a
persistent CASTV2 socket, so we cache the connected device per host
(``_CONNECTIONS``) and reuse it across transport calls.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from ..errors import NotSupportedError, ProviderError
from .base import DeviceInfo, PlaybackDevice, PlaybackStatus

log = logging.getLogger(__name__)

# Standard Chromecast control port; a discovered device carries its real port in
# DeviceInfo.raw, but a bare host (manual add / control-by-host) uses this.
_CAST_PORT = 8009

_CONNECTIONS: dict[str, Any] = {}  # host -> connected pychromecast.Chromecast
# _CONN_LOCK only guards the dicts (held for an instant, never across I/O); each
# host connects under its own lock so one dead device can't stall the others.
_CONN_LOCK = threading.Lock()
_HOST_LOCKS: dict[str, threading.Lock] = {}


def _host_lock(host: str) -> threading.Lock:
    with _CONN_LOCK:
        lock = _HOST_LOCKS.get(host)
        if lock is None:
            lock = _HOST_LOCKS[host] = threading.Lock()
        return lock

# Connections are bounded: a dead address must fail fast (not retry forever),
# a lost socket is dropped (not reconnected behind our back), and a connection
# nobody uses goes away. The next real use reconnects fresh.
CONNECT_TRIES = 2
CONNECT_RETRY_WAIT_S = 3.0
CONNECT_TIMEOUT_S = 8.0
IDLE_CLOSE_S = 600.0  # safety net only: inactive outputs are released at once
REAP_EVERY_S = 30.0

_LAST_USED: dict[str, float] = {}
_busy_provider: Callable[[], set[str]] | None = None
_reaper: threading.Thread | None = None


def _pychromecast() -> Any | None:
    try:
        import pychromecast
    except ImportError:
        return None
    return pychromecast


def available() -> bool:
    """True when the optional Cast dependency is installed."""
    return _pychromecast() is not None


def _cast_host(info: Any) -> str | None:
    """Best-effort host from a pychromecast CastInfo across library versions."""
    host = getattr(info, "host", None)
    if host:
        return host
    # Older/newer variants keep it inside the services set.
    for svc in getattr(info, "services", None) or []:
        h = getattr(svc, "host", None)
        if h:
            return h
    return None


def discover_cast(timeout: float = 4.0) -> list[DeviceInfo]:
    """List Cast devices on the LAN via mDNS, without connecting to each.

    Never raises: any failure (dependency missing, multicast blocked) degrades
    to an empty list, matching ``discover_wiim``.
    """
    pc = _pychromecast()
    if pc is None:
        return []
    infos: list[DeviceInfo] = []
    try:
        import zeroconf
        from pychromecast.discovery import CastBrowser, SimpleCastListener

        zc = zeroconf.Zeroconf()
        browser = CastBrowser(SimpleCastListener(), zc)
        browser.start_discovery()
        try:
            # SimpleCastListener with no callbacks just populates browser.devices.
            import time

            time.sleep(timeout)
            for ci in list(browser.devices.values()):
                host = _cast_host(ci)
                if not host:
                    continue
                raw_uid = getattr(ci, "uuid", None)
                uid = str(raw_uid) if raw_uid else ""
                infos.append(
                    DeviceInfo(
                        id=uid or host,
                        name=getattr(ci, "friendly_name", None) or host,
                        host=host,
                        kind="cast",
                        model=getattr(ci, "model_name", None),
                        raw={"port": getattr(ci, "port", _CAST_PORT), "uuid": uid,
                             "cast_type": getattr(ci, "cast_type", None) or "cast"},
                    )
                )
        finally:
            browser.stop_discovery()
            zc.close()
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        log.debug("chromecast discovery failed: %s", exc)
    return infos


def touch(host: str) -> None:
    """Record real use of ``host``'s connection (resets its idle clock)."""
    _LAST_USED[host] = time.monotonic()


def set_busy_provider(fn: Callable[[], set[str]] | None) -> None:
    """``fn()`` returns the hosts with a running device queue; the idle reaper
    never closes those."""
    global _busy_provider
    _busy_provider = fn


def _disconnect(cast: Any) -> None:
    """Close a connection without blocking (never raises)."""
    try:
        try:
            cast.disconnect(timeout=0)  # pychromecast >= 14
        except TypeError:
            cast.disconnect(blocking=False)
    except Exception:  # noqa: BLE001
        pass


def _is_dead(cast: Any) -> bool:
    """True when a cached connection's socket thread has stopped (a LOST/FAILED
    event can be missed; never hand out a corpse)."""
    sc = getattr(cast, "socket_client", None)
    if sc is None:
        return False
    stop = getattr(sc, "stop", None)
    if stop is not None and hasattr(stop, "is_set") and stop.is_set():
        return True
    alive = getattr(sc, "is_alive", None)
    return bool(callable(alive) and not alive())


def _peek(host: str) -> Any | None:
    """The cached live connection for ``host`` (evicting a dead one); never connects."""
    with _CONN_LOCK:
        cast = _CONNECTIONS.get(host)
        if cast is None:
            return None
        if not _is_dead(cast):
            return cast
        _CONNECTIONS.pop(host, None)
        _LAST_USED.pop(host, None)
    _disconnect(cast)
    return None


def has_connection(host: str) -> bool:
    """True when Harmony currently holds a connection to ``host``."""
    return _peek(host) is not None


class _ConnListener:
    """Drops a connection the moment its socket is lost or fails, instead of
    letting pychromecast reconnect forever in the background."""

    _DEAD = ("LOST", "FAILED", "FAILED_RESOLVE", "DISCONNECTED")

    def __init__(self, host: str) -> None:
        self.host = host
        self.cast: Any = None
        self.dead = threading.Event()

    def new_connection_status(self, status: Any) -> None:
        name = str(getattr(status, "status", ""))
        cast = self.cast
        if name not in self._DEAD:
            if cast is None and name == "CONNECTED":
                self.dead.clear()  # a retry succeeded after an earlier FAILED
            return
        self.dead.set()
        if cast is None:
            return  # still connecting: _connect decides once wait() returns
        log.info("chromecast %s: connection %s — dropped", self.host, status.status)
        with _CONN_LOCK:  # held only for an instant, never across a connect
            if _CONNECTIONS.get(self.host) is cast:
                del _CONNECTIONS[self.host]
                _LAST_USED.pop(self.host, None)
        _disconnect(cast)


def _make_chromecast(pc: Any, host: str, port: int, uid: Any, model: str | None,
                     name: str | None, cast_type: str | None) -> Any:
    """Build a Chromecast with ``cast_type`` already known. Without it
    pychromecast fetches /setup/eureka_info over HTTPS with a 30 s timeout inside
    the constructor (measured 38 s against a dead address)."""
    models = pc.models
    info = models.CastInfo({models.HostServiceInfo(host, port)}, uid, model, name, host, port,
                           cast_type or ("group" if int(port) != _CAST_PORT else "cast"), None)
    return pc.Chromecast(info, tries=CONNECT_TRIES, retry_wait=CONNECT_RETRY_WAIT_S,
                         timeout=CONNECT_TIMEOUT_S)


def _connect(host: str, port: int = _CAST_PORT, uuid: str | None = None,
             model: str | None = None, name: str | None = None,
             cast_type: str | None = None) -> Any:
    """Get a connected Chromecast for ``host``, cached and reused. Bounded: a
    dead address fails in seconds, and only blocks callers of that same host."""
    with _host_lock(host):
        cast = _peek(host)
        if cast is not None:
            return cast  # (callers touch(): a passive poll mustn't count as use)
        pc = _pychromecast()
        if pc is None:
            raise NotSupportedError("Chromecast support needs the 'pychromecast' dependency")
        cast = None
        try:
            uid = None
            if uuid:
                import uuid as _uuidmod

                try:
                    uid = _uuidmod.UUID(uuid)
                except ValueError:
                    uid = None
            cast = _make_chromecast(pc, host, int(port or _CAST_PORT), uid, model, name, cast_type)
            listener = _ConnListener(host)
            cast.register_connection_listener(listener)
            cast.wait(timeout=CONNECT_TIMEOUT_S)  # raises if no status arrived
            # A FAILED from an early try is not fatal if a later try connected:
            # judge by where the connection ended up.
            sc = getattr(cast, "socket_client", None)
            connected = getattr(sc, "is_connected", True)  # a property in pychromecast
            if callable(connected):
                connected = connected()
            if listener.dead.is_set() or not connected:
                raise ConnectionError("connection failed")
            listener.cast = cast
        except Exception as exc:  # noqa: BLE001
            if cast is not None:
                _disconnect(cast)
            raise ProviderError(f"couldn't connect to Chromecast at {host}: {exc}") from exc
        with _CONN_LOCK:
            _CONNECTIONS[host] = cast
            _LAST_USED[host] = time.monotonic()
        _ensure_reaper()
        return cast


def _reap_idle(now: float | None = None, idle_s: float = IDLE_CLOSE_S) -> list[str]:
    """Disconnect connections unused for ``idle_s`` with no running queue."""
    now = time.monotonic() if now is None else now
    try:
        busy = _busy()
    except Exception:  # noqa: BLE001 - when unsure, don't close anything
        return []
    with _CONN_LOCK:
        idle = [h for h in _CONNECTIONS
                if h not in busy and now - _LAST_USED.get(h, 0.0) >= idle_s]
    for h in idle:
        log.info("chromecast %s: idle — closing connection", h)
        _forget(h)
    return idle


def _busy() -> set[str]:
    return set(_busy_provider()) if _busy_provider else set()


def release(host: str) -> bool:
    """Disconnect and forget ``host`` now unless it's an active output.

    Harmony only talks to a cast device while it is the active output; call this
    the moment it stops being one. Returns True when a connection was closed."""
    try:
        if host in _busy():
            return False
    except Exception:  # noqa: BLE001 - when unsure, don't close anything
        return False
    with _CONN_LOCK:
        had = host in _CONNECTIONS
    _forget(host)
    return had


def _ensure_reaper() -> None:
    global _reaper
    if _reaper is not None and _reaper.is_alive():
        return

    def loop() -> None:
        while True:
            time.sleep(REAP_EVERY_S)
            _reap_idle()

    _reaper = threading.Thread(target=loop, daemon=True, name="harmony-cast-reaper")
    _reaper.start()


#: The Default Media Receiver that ``play_media`` launches (pychromecast's
#: ``APP_MEDIA_RECEIVER``).
_MEDIA_RECEIVER_APP_ID = "CC1AD845"


def _forget(host: str) -> None:
    with _CONN_LOCK:
        cast = _CONNECTIONS.pop(host, None)
        _LAST_USED.pop(host, None)
    if cast is not None:
        _disconnect(cast)


class _EndWatch:
    """Latches the receiver's own "this track finished" report.

    The default receiver pushes ``IDLE`` with ``idleReason=FINISHED`` when a
    track plays to its natural end, but pychromecast clears ``idle_reason`` on
    the next status message that lacks one, so a poll can miss it. Listening
    to every status message keeps the fact until the next load or playback."""

    def __init__(self, seed: bool = False,
                 receiver: Callable[[], Any] | None = None) -> None:
        self.ended = seed
        # Identity of the receiver app instance the connection is talking to
        # (its session id + transport id). Media session ids restart from 1 when
        # the receiver app is relaunched on the same connection, so an id alone
        # could match a brand-new session's FINISHED; the ignore is therefore
        # tied to the app instance it was recorded in.
        self._receiver = receiver
        self.ignore_receiver: Any = None
        # The media session that was current when the latch was last reset (a new
        # load). pychromecast re-fires its listeners on every status message,
        # including empty updates and GET_STATUS replies that re-report the cached
        # IDLE/FINISHED of the *previous* session; those must not re-arm the latch.
        self.ignore_session: Any = None

    def _receiver_id(self) -> Any:
        if self._receiver is None:
            return None
        try:
            return self._receiver()
        except Exception:  # noqa: BLE001 - identity is best effort
            return None

    def reset(self, session_id: Any) -> None:
        """A new load starts: forget the finish and ignore ``session_id``'s (as
        reported by the receiver app instance that is current right now)."""
        self.ended = False
        self.ignore_session = session_id
        self.ignore_receiver = self._receiver_id()

    def new_media_status(self, status: Any) -> None:
        state = getattr(status, "player_state", None)
        if state == "IDLE":
            if getattr(status, "idle_reason", None) == "FINISHED":
                stale = (self.ignore_session is not None
                         and getattr(status, "media_session_id", None) == self.ignore_session)
                if stale and self.ignore_receiver is not None:
                    # A relaunched receiver app is a new session even if its
                    # media ids collide with the old one's.
                    now = self._receiver_id()
                    stale = now is None or now == self.ignore_receiver
                if not stale:
                    self.ended = True
        elif state in ("PLAYING", "BUFFERING", "PAUSED", "LOADING"):
            self.ended = False


def _end_watch(cast: Any) -> _EndWatch | None:
    """The cast connection's end-of-track watcher (registered once), or None when
    the controller can't take listeners."""
    watch = getattr(cast, "_harmony_end_watch", None)
    if isinstance(watch, _EndWatch):
        return watch
    mc = cast.media_controller
    register = getattr(mc, "register_status_listener", None)
    if register is None:
        return None
    live = getattr(mc, "status", None)
    # A connection opened after the track already finished still reports it.
    def receiver() -> Any:
        status = getattr(cast, "status", None)
        ids = (getattr(status, "session_id", None), getattr(status, "transport_id", None))
        return ids if any(ids) else None

    watch = _EndWatch(seed=(getattr(live, "player_state", None) == "IDLE"
                            and getattr(live, "idle_reason", None) == "FINISHED"),
                      receiver=receiver)
    try:
        register(watch)
        cast._harmony_end_watch = watch
    except Exception:  # noqa: BLE001
        return None
    return watch


class ChromecastDevice(PlaybackDevice):
    """A Chromecast/Google TV renderer, controlled over CASTV2."""

    def __init__(self, host: str, info: DeviceInfo | None = None) -> None:
        raw = (info.raw if info else {}) or {}
        self._host = host
        self._port = int(raw.get("port") or _CAST_PORT)
        self._uuid = raw.get("uuid") or None
        self._model = info.model if info else None
        self._cast_type = raw.get("cast_type") or None
        self._name = info.name if info else host
        self.info = info or DeviceInfo(id=host, name=host, host=host, kind="cast")

    def _cast(self, *, use: bool = True) -> Any:
        cast = _connect(self._host, self._port, self._uuid, self._model, self._name,
                        self._cast_type)
        if use:
            touch(self._host)
        return cast

    def _mc(self) -> Any:
        return self._cast().media_controller

    def play_url(self, url: str, *, mime: str | None = "audio/mpeg", title: str | None = None,
                 artist: str | None = None, album: str | None = None, art_url: str | None = None,
                 duration_s: int | None = None, **_extra: Any) -> None:
        cast = self._cast()
        mc = cast.media_controller
        watch = _end_watch(cast)
        if watch is not None:
            # A new load: forget the previous track's "finished" (the controller
            # still holds its stale status, which is why status() reads the latch
            # and not the controller's own idle_reason). A late status for the old
            # media session must not re-arm it, so remember that session's id.
            watch.reset(getattr(getattr(mc, "status", None), "media_session_id", None))
        # MusicTrackMediaMetadata (type 3) → the device's now-playing card shows
        # title/artist/album + cover art.
        metadata: dict[str, Any] = {"metadataType": 3}
        if title:
            metadata["title"] = title
        if artist:
            metadata["artist"] = artist
        if album:
            metadata["albumName"] = album
        if art_url:
            metadata["images"] = [{"url": art_url}]
        try:
            mc.play_media(url, content_type=mime or "audio/mpeg", title=title, thumb=art_url,
                          metadata=metadata)
            mc.block_until_active(timeout=10)
        except Exception as exc:  # noqa: BLE001
            _forget(self._host)
            raise ProviderError(f"Chromecast play failed: {exc}") from exc

    def status(self, *, connect: bool = True) -> PlaybackStatus:
        """The device's state. ``connect=False`` never opens a connection: a
        cast Harmony isn't connected to (after Stop, or never used) reports
        "stopped" instead, so background polling can't reconnect to it."""
        cast = None if connect else _peek(self._host)  # one read: no check-then-act gap
        if not connect and cast is None:
            return PlaybackStatus(state="stopped", volume=None, muted=False,
                                  position_s=None, duration_s=None,
                                  raw={"connected": False})
        try:
            if cast is None:
                cast = self._cast()
            # pychromecast only refreshes media_controller.status (and its
            # current_time) when asked; without this the reported position stays
            # frozen at the last event, so the Now Playing progress bar never
            # advances while casting. Best-effort — a poll that can't refresh
            # just reads the previous values.
            try:
                cast.media_controller.update_status()
            except Exception:  # noqa: BLE001 - a stale read is fine; don't fail the poll
                pass
            mc = cast.media_controller.status
            cs = cast.status
            state_map = {"PLAYING": "playing", "PAUSED": "paused", "IDLE": "stopped",
                         "BUFFERING": "playing"}
            vol = int(round((cs.volume_level or 0) * 100)) if cs else None
            idle_reason = getattr(mc, "idle_reason", None)
            watch = _end_watch(cast)
            if watch is not None:
                ended = watch.ended
            else:  # no listener support: read the live status only
                ended = getattr(mc, "player_state", "") == "IDLE" and idle_reason == "FINISHED"
            return PlaybackStatus(
                state=state_map.get(getattr(mc, "player_state", ""), "unknown"),
                volume=vol,
                muted=bool(getattr(cs, "volume_muted", False)),
                position_s=int(mc.current_time) if getattr(mc, "current_time", None) else None,
                duration_s=int(mc.duration) if getattr(mc, "duration", None) else None,
                title=getattr(mc, "title", None),
                artist=None,
                raw={"idle_reason": idle_reason, "ended": bool(ended)},
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Chromecast status failed: {exc}") from exc

    def _mc_ready(self, timeout: float = 4.0) -> Any:
        """The media controller, with its session known.

        pychromecast's transport commands need the current media session id;
        on a connection that didn't start the cast (another process, or a fresh
        connection after a reconnect) it isn't known yet, and the command is
        silently dropped ("no session is active") — Stop did nothing and the
        cast kept playing. Ask the device for its status first."""
        mc = self._mc()
        status = getattr(mc, "status", None)
        if status is None or getattr(status, "media_session_id", None) is None:
            done = threading.Event()
            try:
                mc.update_status(callback_function=lambda *_a: done.set())
                done.wait(timeout)
            except Exception as exc:  # noqa: BLE001 - the command below reports the failure
                log.debug("chromecast %s: status refresh failed: %s", self._host, exc)
        return mc

    def pause(self) -> None:
        self._mc_ready().pause()

    def resume(self) -> None:
        self._mc_ready().play()

    def stop(self) -> None:
        """Stop casting: stop the media, then close the media receiver app so
        the TV/speaker really leaves the cast (only when it's the receiver
        Harmony uses — never some other app the user is running there)."""
        cast = _peek(self._host)
        if cast is None:
            return  # never connect just to stop: nothing of ours is playing there
        try:
            try:
                self._mc_ready().stop()
            except Exception as exc:  # noqa: BLE001 - quitting the app below still stops it
                log.debug("chromecast %s: media stop failed: %s", self._host, exc)
            if getattr(cast, "app_id", None) == _MEDIA_RECEIVER_APP_ID:
                cast.quit_app()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Chromecast stop failed: {exc}") from exc
        finally:
            _forget(self._host)

    def set_volume(self, level: int) -> None:
        self._cast().set_volume(max(0, min(100, int(level))) / 100.0)

    def set_muted(self, muted: bool) -> None:
        self._cast().set_volume_muted(bool(muted))

    def seek(self, position_s: int) -> None:
        self._mc().seek(max(0, int(position_s)))

    def next(self) -> None:
        raise NotSupportedError("Chromecast default receiver has no queue-next")

    def previous(self) -> None:
        raise NotSupportedError("Chromecast default receiver has no queue-previous")
