"""Device-agnostic playback interface: enumerate, push media, control transport.

Concrete backends (``wiim.py`` today; UPnP/Chromecast are future candidates,
see ``docs/design/playback.md``) implement :class:`PlaybackDevice` against
plain dataclasses that carry no service-specific shape, so the rest of the
engine never has to branch on which renderer a device is.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DeviceInfo:
    """Identity of a playback device, independent of how it was found."""

    id: str  # stable id (uuid from getStatusEx, else the host)
    name: str
    host: str  # ip/hostname
    kind: str = "wiim"  # backend discriminator, future-proofing for UPnP/Chromecast
    model: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class PlaybackStatus:
    """A snapshot of what a device is doing right now."""

    state: str  # "playing" | "paused" | "stopped" | "unknown"
    volume: int | None  # 0..100
    muted: bool
    position_s: int | None
    duration_s: int | None
    title: str | None = None
    artist: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class PlaybackDevice(ABC):
    """A renderer that can play a stream URL and be transport-controlled.

    Implementations raise ``harmony.errors.ProviderError`` on transport
    failure and ``harmony.errors.NotSupportedError`` for an operation the
    device has no equivalent for.
    """

    info: DeviceInfo

    @abstractmethod
    def status(self) -> PlaybackStatus: ...

    @abstractmethod
    def play_url(
        self,
        url: str,
        *,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        art_url: str | None = None,
        duration_s: int | None = None,
        mime: str | None = None,
    ) -> None:
        """Start playing a stream URL.

        The optional track metadata is for renderers that display it themselves
        (Chromecast's on-screen card). Backends that read metadata from the
        stream instead (LinkPlay via ICY) may ignore it.
        """

    @abstractmethod
    def pause(self) -> None: ...

    @abstractmethod
    def resume(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def set_volume(self, level: int) -> None:
        """Set volume 0..100, clamping out-of-range values."""

    @abstractmethod
    def set_muted(self, muted: bool) -> None: ...

    @abstractmethod
    def next(self) -> None: ...

    @abstractmethod
    def previous(self) -> None: ...
