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
from typing import Any

from ..errors import NotSupportedError, ProviderError
from .base import DeviceInfo, PlaybackDevice, PlaybackStatus

log = logging.getLogger(__name__)

# Standard Chromecast control port; a discovered device carries its real port in
# DeviceInfo.raw, but a bare host (manual add / control-by-host) uses this.
_CAST_PORT = 8009

_CONNECTIONS: dict[str, Any] = {}  # host -> connected pychromecast.Chromecast
_CONN_LOCK = threading.Lock()


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
                infos.append(
                    DeviceInfo(
                        id=str(getattr(ci, "uuid", host)),
                        name=getattr(ci, "friendly_name", None) or host,
                        host=host,
                        kind="cast",
                        model=getattr(ci, "model_name", None),
                        raw={"port": getattr(ci, "port", _CAST_PORT), "uuid": str(getattr(ci, "uuid", ""))},
                    )
                )
        finally:
            browser.stop_discovery()
            zc.close()
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        log.debug("chromecast discovery failed: %s", exc)
    return infos


def _connect(host: str, port: int = _CAST_PORT, uuid: str | None = None,
             model: str | None = None, name: str | None = None) -> Any:
    """Get a connected Chromecast for ``host``, cached and reused."""
    with _CONN_LOCK:
        cast = _CONNECTIONS.get(host)
        if cast is not None:
            return cast
        pc = _pychromecast()
        if pc is None:
            raise NotSupportedError("Chromecast support needs the 'pychromecast' dependency")
        try:
            uid = None
            if uuid:
                import uuid as _uuidmod

                try:
                    uid = _uuidmod.UUID(uuid)
                except ValueError:
                    uid = None
            cast = pc.get_chromecast_from_host((host, int(port or _CAST_PORT), uid, model, name))
            cast.wait(timeout=6)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"couldn't connect to Chromecast at {host}: {exc}") from exc
        _CONNECTIONS[host] = cast
        return cast


def _forget(host: str) -> None:
    with _CONN_LOCK:
        cast = _CONNECTIONS.pop(host, None)
    if cast is not None:
        try:
            cast.disconnect(blocking=False)
        except Exception:  # noqa: BLE001
            pass


class ChromecastDevice(PlaybackDevice):
    """A Chromecast/Google TV renderer, controlled over CASTV2."""

    def __init__(self, host: str, info: DeviceInfo | None = None) -> None:
        raw = (info.raw if info else {}) or {}
        self._host = host
        self._port = int(raw.get("port") or _CAST_PORT)
        self._uuid = raw.get("uuid") or None
        self._model = info.model if info else None
        self._name = info.name if info else host
        self.info = info or DeviceInfo(id=host, name=host, host=host, kind="cast")

    def _cast(self) -> Any:
        return _connect(self._host, self._port, self._uuid, self._model, self._name)

    def _mc(self) -> Any:
        return self._cast().media_controller

    def play_url(self, url: str, *, mime: str | None = "audio/mpeg", title: str | None = None,
                 artist: str | None = None, album: str | None = None, art_url: str | None = None,
                 duration_s: int | None = None, **_extra: Any) -> None:
        mc = self._mc()
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

    def status(self) -> PlaybackStatus:
        try:
            cast = self._cast()
            mc = cast.media_controller.status
            cs = cast.status
            state_map = {"PLAYING": "playing", "PAUSED": "paused", "IDLE": "stopped",
                         "BUFFERING": "playing"}
            vol = int(round((cs.volume_level or 0) * 100)) if cs else None
            return PlaybackStatus(
                state=state_map.get(getattr(mc, "player_state", ""), "unknown"),
                volume=vol,
                muted=bool(getattr(cs, "volume_muted", False)),
                position_s=int(mc.current_time) if getattr(mc, "current_time", None) else None,
                duration_s=int(mc.duration) if getattr(mc, "duration", None) else None,
                title=getattr(mc, "title", None),
                artist=None,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Chromecast status failed: {exc}") from exc

    def pause(self) -> None:
        self._mc().pause()

    def resume(self) -> None:
        self._mc().play()

    def stop(self) -> None:
        try:
            self._mc().stop()
        finally:
            _forget(self._host)

    def set_volume(self, level: int) -> None:
        self._cast().set_volume(max(0, min(100, int(level))) / 100.0)

    def set_muted(self, muted: bool) -> None:
        self._cast().set_volume_muted(bool(muted))

    def next(self) -> None:
        raise NotSupportedError("Chromecast default receiver has no queue-next")

    def previous(self) -> None:
        raise NotSupportedError("Chromecast default receiver has no queue-previous")
