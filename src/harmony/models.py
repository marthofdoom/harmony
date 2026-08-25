"""Service-neutral domain models.

Every provider normalises its own payloads into these types so that matching,
sync, and the UI never have to care which backend a row came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Service(str, Enum):
    YTMUSIC = "ytmusic"
    QOBUZ = "qobuz"

    @property
    def label(self) -> str:
        return {"ytmusic": "YouTube Music", "qobuz": "Qobuz"}[self.value]


@dataclass(slots=True)
class Artist:
    id: str
    name: str
    service: Service
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class Album:
    id: str
    title: str
    service: Service
    artists: list[str] = field(default_factory=list)
    year: int | None = None
    track_count: int | None = None
    artwork_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def artist_name(self) -> str:
        return ", ".join(self.artists)


@dataclass(slots=True)
class Track:
    """A single playable track on one service.

    ``id`` is the provider-native identifier: a videoId on YouTube Music, a
    numeric track id on Qobuz.
    """

    id: str
    title: str
    service: Service
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    duration_s: int | None = None
    isrc: str | None = None
    year: int | None = None
    track_number: int | None = None
    artwork_url: str | None = None
    explicit: bool = False
    play_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def artist_name(self) -> str:
        return ", ".join(self.artists)

    @property
    def duration_text(self) -> str:
        if not self.duration_s:
            return "--:--"
        minutes, seconds = divmod(int(self.duration_s), 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def key(self) -> tuple[str, str]:
        return (self.service.value, self.id)


@dataclass(slots=True)
class Playlist:
    id: str
    title: str
    service: Service
    description: str = ""
    track_count: int | None = None
    owner: str | None = None
    public: bool = False
    artwork_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class SearchResults:
    tracks: list[Track] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    artists: list[Artist] = field(default_factory=list)
    playlists: list[Playlist] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.tracks or self.albums or self.artists or self.playlists)
