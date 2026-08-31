"""Casting to LAN devices (WiiM/UPnP) from the web server.

Reuses the gi-free relay + device backends -- the single-track, queue-less
subset of the desktop's play-to-device. (The multi-track server-side cast queue
lives in AppState and is a later slice.) GTK-free.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class CastController:
    """Plays a resolved track on a network device via the relay, and controls it."""

    def __init__(self, resolve_source: Callable[[str, str], Any]) -> None:
        # resolve_source(service, track_id) -> StreamSource (url/headers/mime).
        self._resolve_source = resolve_source
        self._relay: Any | None = None
        self._session: Any | None = None
        self._upnp_cache: dict[str, Any] = {}

    def _relay_server(self) -> Any:
        if self._relay is None:
            from harmony.playback import RelayServer

            relay = RelayServer()
            relay.start()
            self._relay = relay
        return self._relay

    def _http_session(self) -> Any:
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _device(self, host: str) -> Any:
        from harmony.playback import WiiMDevice

        return WiiMDevice(host, session=self._http_session())

    def _upnp_renderer(self, host: str) -> Any | None:
        if host in self._upnp_cache:
            return self._upnp_cache[host]
        renderer = None
        try:
            from harmony.playback import upnp

            description = upnp.description_url_for(host)
            service = upnp.find_avtransport(description, self._http_session()) if description else None
            if service is not None:
                renderer = upnp.UpnpRenderer(service, session=self._http_session())
        except Exception as exc:  # noqa: BLE001 - UPnP is optional; fall back to httpapi
            log.debug("UPnP probe failed for %s: %s", host, exc)
        self._upnp_cache[host] = renderer
        return renderer

    def cast(self, host: str, service: str, track_id: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        meta = meta or {}
        source = self._resolve_source(service, track_id)
        relay = self._relay_server()
        renderer = self._upnp_renderer(host)
        if renderer is not None:
            token = relay.register(lambda: source, title=meta.get("title"),
                                   artist=meta.get("artist"), allow_icy=False)
            url = relay.url_for(token, host)
            renderer.play_media(
                url, title=meta.get("title"), artist=meta.get("artist"),
                album=meta.get("album"), art_url=meta.get("art_url"),
                duration_s=meta.get("duration_s"), mime=source.mime_type or "audio/mpeg",
            )
        else:
            token = relay.register(lambda: source, title=meta.get("title"),
                                   artist=meta.get("artist"), allow_icy=True)
            url = relay.url_for(token, host)
            self._device(host).play_url(url)
        return {"ok": True, "host": host}

    def control(self, host: str, action: str, level: int | None = None) -> dict[str, Any]:
        device = self._device(host)
        if action == "pause":
            device.pause()
        elif action == "resume":
            device.resume()
        elif action == "stop":
            device.stop()
        elif action == "volume":
            device.set_volume(int(level or 0))
        else:
            raise ValueError(f"unknown action {action}")
        return {"ok": True}

    def status(self, host: str) -> dict[str, Any]:
        st = self._device(host).status()
        return {
            "state": getattr(st, "state", None),
            "position_s": getattr(st, "position_s", None),
            "duration_s": getattr(st, "duration_s", None),
            "volume": getattr(st, "volume", None),
        }
