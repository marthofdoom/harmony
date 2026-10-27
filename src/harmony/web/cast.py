"""Casting to LAN devices (WiiM/UPnP) from the web server.

Reuses the gi-free relay + device backends to play ONE track and control the
device; the multi-track queue + auto-advance on top of this is
``harmony.web.device_queue``. GTK-free.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from harmony.streamio import is_file_url

log = logging.getLogger(__name__)

# A resolved provider URL (Qobuz signed URL, googlevideo) is only good for a
# while, but a cast device fetches the track in Range chunks across the whole
# song (and again on every seek). When the URL doesn't say when it expires,
# re-resolve once it is this old so a late chunk doesn't hit an expired link and
# end the track early. (A 403/410 from the CDN is the backstop either way.)
SOURCE_STALE_S = 150.0
# When the URL *does* carry its expiry, renew this long before it (or a quarter
# of its remaining life, if that is shorter).
REFRESH_MARGIN_S = 60.0
# After a failed re-resolve, leave the provider alone this long: otherwise every
# chunk request the device makes retries a full (slow) extraction.
FAIL_BACKOFF_S = 20.0

_EXPIRY_QUERY_KEYS = ("expire", "etsp", "Expires")
_EXPIRY_PATH = re.compile(r"/expire/(\d+)(?:/|$)")


def url_expiry_epoch(url: str) -> float | None:
    """When a signed URL stops working (epoch seconds), read from the URL itself:
    googlevideo's ``expire=`` (query or ``/expire/N/`` path form), Qobuz's
    ``etsp=``, or a CDN-style ``Expires=``. None when the URL doesn't say."""
    try:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
    except ValueError:
        return None
    candidates: list[str] = []
    for key in _EXPIRY_QUERY_KEYS:
        candidates += query.get(key, [])
    m = _EXPIRY_PATH.search(parts.path)
    if m:
        candidates.append(m.group(1))
    for raw in candidates:
        try:
            value = float(raw)
        except ValueError:
            continue
        if 1e9 < value < 4e9:  # a plausible epoch in seconds, not a ttl or ms value
            return value
    return None


class _FreshSource:
    """A resolved stream that re-resolves itself shortly before it expires.

    ``get()`` is the relay's per-request resolver; ``refresh()`` forces a new
    resolve (the relay calls it when the upstream says the URL expired).
    The URL's own expiry decides when it is stale (a googlevideo URL lives hours,
    so it is *not* re-extracted every couple of minutes); only a URL that doesn't
    carry one falls back to ``SOURCE_STALE_S``. Library files (``file://``) never
    expire and are never re-resolved. A failed re-resolve backs off instead of
    being retried by every chunk request."""

    def __init__(self, resolve: Callable[[], Any], source: Any) -> None:
        self._resolve = resolve
        self._lock = threading.Lock()
        self._retry_at = 0.0
        self._set(source)

    def _set(self, source: Any) -> None:
        self._source = source
        self._at = time.monotonic()
        exp = None if is_file_url(source.url) else url_expiry_epoch(source.url)
        life = None if exp is None else exp - time.time()
        # Monotonic deadline for the *known* expiry; an already-past or absent
        # one means "unknown" (age-based), so an odd value can't cause a refresh loop.
        self._expiry: float | None = (self._at + life) if life is not None and life > 0 else None

    def _stale(self) -> bool:
        now = time.monotonic()
        if self._expiry is not None:
            margin = min(REFRESH_MARGIN_S, 0.25 * (self._expiry - self._at))
            return now >= self._expiry - margin
        return now - self._at > SOURCE_STALE_S

    def get(self) -> Any:
        with self._lock:
            if (not is_file_url(self._source.url) and self._stale()
                    and time.monotonic() >= self._retry_at):
                self._renew()
            return self._source

    def refresh(self) -> Any:
        """Force a re-resolve (the CDN said 403/410). Honours the failure
        back-off like ``get()``: with a dead provider every chunk request would
        otherwise run a slow forced resolve under the lock. Inside the window it
        raises (the relay answers 502) without calling the resolver; once the
        window has passed a refresh is allowed again."""
        with self._lock:
            if time.monotonic() < self._retry_at:
                raise RuntimeError("stream re-resolve is backing off after a failure")
            self._renew(strict=True)
            return self._source

    def _renew(self, strict: bool = False) -> None:
        try:
            self._set(self._resolve())
            self._retry_at = 0.0
        except Exception as exc:  # noqa: BLE001
            self._retry_at = time.monotonic() + FAIL_BACKOFF_S
            if strict:
                raise
            # Keep serving the old URL (it may still work); retry after a pause.
            log.warning("cast: re-resolving a stale stream failed: %s", exc)


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

    def _device(self, host: str, kind: str = "wiim", device_info: dict[str, Any] | None = None) -> Any:
        if kind == "cast":
            from harmony.playback import ChromecastDevice
            from harmony.playback.base import DeviceInfo

            raw = {"port": (device_info or {}).get("port"), "uuid": (device_info or {}).get("uuid")}
            info = DeviceInfo(id=host, name=(device_info or {}).get("name") or host, host=host,
                              kind="cast", raw={k: v for k, v in raw.items() if v})
            return ChromecastDevice(host, info=info)
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

    def cast(self, host: str, service: str, track_id: str, meta: dict[str, Any] | None = None,
             kind: str = "wiim", device_info: dict[str, Any] | None = None) -> dict[str, Any]:
        meta = meta or {}
        source = self._resolve_source(service, track_id)
        relay = self._relay_server()
        fresh = _FreshSource(lambda: self._resolve_source(service, track_id), source)
        if kind == "cast":
            # Chromecast plays the relay's HTTP URL through its default receiver.
            token = relay.register(fresh.get, title=meta.get("title"), artist=meta.get("artist"),
                                   allow_icy=False, refresh=fresh.refresh)
            url = relay.url_for(token, host)
            self._device(host, kind, device_info).play_url(
                url, mime=source.mime_type or "audio/mpeg", title=meta.get("title"),
                artist=meta.get("artist"), album=meta.get("album"),
                art_url=meta.get("art_url"), duration_s=meta.get("duration_s"),
            )
            return {"ok": True, "host": host}
        renderer = self._upnp_renderer(host)
        if renderer is not None:
            token = relay.register(fresh.get, title=meta.get("title"), artist=meta.get("artist"),
                                   allow_icy=False, refresh=fresh.refresh)
            url = relay.url_for(token, host)
            renderer.play_media(
                url, title=meta.get("title"), artist=meta.get("artist"),
                album=meta.get("album"), art_url=meta.get("art_url"),
                duration_s=meta.get("duration_s"), mime=source.mime_type or "audio/mpeg",
            )
        else:
            token = relay.register(fresh.get, title=meta.get("title"), artist=meta.get("artist"),
                                   allow_icy=True, refresh=fresh.refresh)
            url = relay.url_for(token, host)
            self._device(host).play_url(url)
        return {"ok": True, "host": host}

    def control(self, host: str, action: str, level: int | None = None,
                kind: str = "wiim", device_info: dict[str, Any] | None = None) -> dict[str, Any]:
        device = self._device(host, kind, device_info)
        if action == "pause":
            device.pause()
        elif action == "resume":
            device.resume()
        elif action == "stop":
            device.stop()
        elif action == "volume":
            device.set_volume(int(level or 0))
        elif action == "mute":
            device.set_muted(bool(level))
        elif action == "seek":
            # UPnP AVTransport where the device speaks it (exact); else the
            # backend's own seek (WiiM httpapi, Chromecast media controller).
            renderer = self._upnp_renderer(host) if kind != "cast" else None
            if renderer is not None:
                renderer.seek(int(level or 0))
            else:
                device.seek(int(level or 0))
        else:
            raise ValueError(f"unknown action {action}")
        return {"ok": True}

    def status(self, host: str, kind: str = "wiim", device_info: dict[str, Any] | None = None,
               *, connect: bool = True) -> dict[str, Any]:
        device = self._device(host, kind, device_info)
        # A Chromecast keeps a session open while connected; an idle poll must
        # not (re)open one — Harmony stays disconnected after Stop.
        st = device.status(connect=connect) if kind == "cast" else device.status()
        raw = getattr(st, "raw", None) or {}
        out = {
            "state": getattr(st, "state", None),
            "position_s": getattr(st, "position_s", None),
            "duration_s": getattr(st, "duration_s", None),
            "volume": getattr(st, "volume", None),
        }
        if kind == "cast" and "ended" in raw:
            # The device's own report: True once it finished the track by itself
            # (the queue advances on this, not on a guess from the position).
            out["ended"] = bool(raw["ended"])
            out["idle_reason"] = raw.get("idle_reason")
        return out
