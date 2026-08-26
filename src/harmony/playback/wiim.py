"""WiiM / LinkPlay on-LAN HTTP API backend.

Every LinkPlay-based renderer (WiiM Mini/Pro/Amp, and the many rebadged
LinkPlay modules) exposes the same plain-HTTP control API:
``http://<host>/httpapi.asp?command=<cmd>``. Newer firmware also serves the
same API over HTTPS on 443 with a self-signed certificate; we try HTTP first
and fall back to HTTPS with verification disabled only because the
certificate is self-signed *by design* on a device that never leaves the
LAN — there is no CA to verify against and no third party in the path.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import requests
import urllib3

from ..errors import ProviderError
from .base import DeviceInfo, PlaybackDevice, PlaybackStatus

log = logging.getLogger(__name__)

_TIMEOUT_S = 5.0

# See the module docstring: verify=False here is a deliberate choice for a
# self-signed, on-LAN-only device, not a suppressed mistake.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_STATE_MAP = {"play": "playing", "pause": "paused", "stop": "stopped", "load": "playing"}


def _decode_hex(value: str | None) -> str | None:
    """LinkPlay hex-encodes Title/Artist/Album; fall back to the raw value if it isn't hex."""
    if not value:
        return None
    try:
        return bytes.fromhex(value).decode("utf-8")
    except ValueError:
        return value


def _ms_to_s(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value) // 1000
    except (TypeError, ValueError):
        return None


def _parse_status(payload: dict[str, Any]) -> PlaybackStatus:
    vol = payload.get("vol")
    return PlaybackStatus(
        state=_STATE_MAP.get(payload.get("status", ""), "unknown"),
        volume=int(vol) if vol not in (None, "") else None,
        muted=payload.get("mute") == "1",
        position_s=_ms_to_s(payload.get("curpos")),
        duration_s=_ms_to_s(payload.get("totlen")),
        title=_decode_hex(payload.get("Title")),
        artist=_decode_hex(payload.get("Artist")),
        raw=payload,
    )


class WiiMDevice(PlaybackDevice):
    """A single WiiM/LinkPlay renderer, addressed by IP or hostname."""

    def __init__(self, host: str, *, session: requests.Session | None = None, info: DeviceInfo | None = None) -> None:
        self.host = host
        self._session = session or requests.Session()
        self._scheme = "http"  # flips to "https" the first time http fails, then sticks
        self._info = info

    @property
    def info(self) -> DeviceInfo:
        if self._info is None:
            self._info = self._fetch_info()
        return self._info

    def _fetch_info(self) -> DeviceInfo:
        payload = self._command("getStatusEx")
        if not isinstance(payload, dict):
            raise ProviderError(f"{self.host}: getStatusEx returned an unexpected payload")
        uuid = payload.get("uuid") or self.host
        name = payload.get("DeviceName") or uuid
        return DeviceInfo(id=uuid, name=name, host=self.host, model=payload.get("hardware"), raw=payload)

    def _url(self, cmd: str, scheme: str) -> str:
        return f"{scheme}://{self.host}/httpapi.asp?command={cmd}"

    def _get(self, cmd: str, scheme: str) -> requests.Response:
        return self._session.get(self._url(cmd, scheme), timeout=_TIMEOUT_S, verify=scheme != "https")

    def _command(self, cmd: str) -> str | dict[str, Any]:
        """GET ``httpapi.asp?command=<cmd>``, falling back http -> https once, then parse the body."""
        try:
            response = self._get(cmd, self._scheme)
        except requests.RequestException as exc:
            if self._scheme == "https":
                raise ProviderError(f"{self.host}: {cmd} failed: {exc}") from exc
            self._scheme = "https"
            try:
                response = self._get(cmd, self._scheme)
            except requests.RequestException as exc2:
                raise ProviderError(f"{self.host}: {cmd} failed over http and https: {exc2}") from exc2
        if not response.ok:
            raise ProviderError(f"{self.host}: {cmd} returned HTTP {response.status_code}")
        text = response.text.strip()
        if text == "OK":
            return text
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"{self.host}: {cmd} returned an unparsable response: {text[:200]!r}") from exc

    def _set(self, sub: str) -> None:
        result = self._command(f"setPlayerCmd:{sub}")
        if result != "OK":
            raise ProviderError(f"{self.host}: setPlayerCmd:{sub} did not confirm (got {result!r})")

    def status(self) -> PlaybackStatus:
        payload = self._command("getPlayerStatus")
        if not isinstance(payload, dict):
            raise ProviderError(f"{self.host}: getPlayerStatus returned an unexpected payload")
        return _parse_status(payload)

    def play_url(self, url: str) -> None:
        self._set(f"play:{quote(url, safe='')}")

    def pause(self) -> None:
        self._set("pause")

    def resume(self) -> None:
        self._set("resume")

    def stop(self) -> None:
        self._set("stop")

    def set_volume(self, level: int) -> None:
        self._set(f"vol:{max(0, min(100, level))}")

    def set_muted(self, muted: bool) -> None:
        self._set(f"mute:{1 if muted else 0}")

    def next(self) -> None:
        self._set("next")

    def previous(self) -> None:
        self._set("prev")


def device_from_host(host: str, session: requests.Session | None = None) -> WiiMDevice:
    """Manually add a device by IP/hostname, bypassing discovery."""
    return WiiMDevice(host, session=session)


__all__ = ["WiiMDevice", "device_from_host"]
