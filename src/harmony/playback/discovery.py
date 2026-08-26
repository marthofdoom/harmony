"""Best-effort SSDP discovery of LinkPlay/WiiM renderers on the LAN.

Runs from a worker thread as part of a "scan for devices" action, so it must
never raise: any socket-level failure (multicast blocked, no network, ...)
degrades to an empty list rather than surfacing to the caller. Manual add via
``wiim.device_from_host`` remains the fallback when discovery finds nothing.
"""

from __future__ import annotations

import logging
import socket
import time
from urllib.parse import urlparse

import requests

from ..errors import ProviderError
from .base import DeviceInfo
from .wiim import WiiMDevice

log = logging.getLogger(__name__)

_SSDP_ADDR = ("239.255.255.250", 1900)
_SEARCH_TARGET = "urn:schemas-upnp-org:device:MediaRenderer:1"
_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    f"ST: {_SEARCH_TARGET}\r\n"
    "MX: 2\r\n"
    "\r\n"
).encode()


def _parse_ssdp_response(text: str) -> str | None:
    """Pull the host out of an SSDP response's LOCATION header, if present.

    Factored out from the socket loop so the parsing logic is unit-testable
    with a canned response string, no real network involved.
    """
    for line in text.splitlines():
        if line.lower().startswith("location:"):
            return urlparse(line.split(":", 1)[1].strip()).hostname
    return None


def _probe(host: str, session: requests.Session) -> DeviceInfo | None:
    """Confirm ``host`` actually answers like a LinkPlay device."""
    try:
        return WiiMDevice(host, session=session).info
    except (ProviderError, requests.RequestException) as exc:
        log.debug("%s did not respond like a LinkPlay device: %s", host, exc)
        return None


def discover_wiim(timeout: float = 3.0) -> list[DeviceInfo]:
    """SSDP M-SEARCH for media renderers, then confirm each via getStatusEx."""
    hosts: set[str] = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(_MSEARCH, _SSDP_ADDR)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, addr = sock.recvfrom(2048)
                except TimeoutError:
                    break
                host = _parse_ssdp_response(data.decode("utf-8", errors="ignore")) or addr[0]
                hosts.add(host)
        finally:
            sock.close()
    except OSError as exc:
        log.debug("SSDP discovery unavailable: %s", exc)
        return []

    session = requests.Session()
    infos = (_probe(host, session) for host in hosts)
    return [info for info in infos if info is not None]
