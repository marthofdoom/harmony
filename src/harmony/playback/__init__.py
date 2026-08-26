"""Play-to-device: hand a stream URL to a hardware renderer instead of decoding it in-app.

``PlaybackDevice`` is the device-agnostic control-plane interface; ``WiiMDevice``
is the first (and so far only) backend, for the WiiM/LinkPlay on-LAN HTTP API.
Engine code only — no UI, no GTK. See ``docs/design/playback.md``.
"""

from __future__ import annotations

from .base import DeviceInfo, PlaybackDevice, PlaybackStatus
from .discovery import discover_wiim
from .wiim import WiiMDevice, device_from_host

__all__ = [
    "DeviceInfo",
    "PlaybackDevice",
    "PlaybackStatus",
    "WiiMDevice",
    "device_from_host",
    "discover_wiim",
]
