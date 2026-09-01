"""Play-to-device: hand a stream URL to a hardware renderer instead of decoding it in-app.

``PlaybackDevice`` is the device-agnostic control-plane interface; ``WiiMDevice``
is the first (and so far only) backend, for the WiiM/LinkPlay on-LAN HTTP API.
Engine code only — no UI, no GTK. See ``docs/design/playback.md``.
"""

from __future__ import annotations

from .base import DeviceInfo, PlaybackDevice, PlaybackStatus
from .chromecast import ChromecastDevice, discover_cast
from .discovery import discover_wiim
from .relay import RelayServer
from .wiim import WiiMDevice, device_from_host

__all__ = [
    "ChromecastDevice",
    "DeviceInfo",
    "PlaybackDevice",
    "PlaybackStatus",
    "RelayServer",
    "WiiMDevice",
    "device_from_host",
    "discover_cast",
    "discover_wiim",
]
