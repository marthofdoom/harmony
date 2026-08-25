"""Shared test fixtures: a duck-typed fake provider.

``harmony.providers.base.MusicProvider`` is owned by another module and may
not exist yet at import time, so this fake implements the ABC's surface by
duck typing only — it is never imported as a subclass of anything from
``harmony.providers``.
"""

from __future__ import annotations

from harmony.models import Playlist, SearchResults, Service, Track


class FakeProvider:
    """A minimal in-memory stand-in for a real MusicProvider.

    ``search`` ignores the query text and just returns the catalog (capped
    to ``limit``) — tests control relevance by choosing what goes in the
    catalog, not by exercising a search index.
    """

    def __init__(self, service: Service, catalog: list[Track] | None = None) -> None:
        self.service = service
        self.catalog: list[Track] = list(catalog or [])
        self.playlists: dict[str, list[Track]] = {}
        self.fail_ids: set[str] = set()
        self._next_id = 1

    def search(self, query: str, *, kinds: tuple[str, ...] = ("tracks",), limit: int = 25) -> SearchResults:
        return SearchResults(tracks=list(self.catalog[:limit]))

    def get_track(self, track_id: str) -> Track:
        for track in self.catalog:
            if track.id == track_id:
                return track
        raise KeyError(track_id)

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        return list(self.playlists.get(playlist_id, []))

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        bucket = self.playlists.setdefault(playlist_id, [])
        present = {t.id for t in bucket}
        for track_id in track_ids:
            if track_id in self.fail_ids:
                raise RuntimeError(f"simulated failure adding {track_id}")
            if track_id in present:
                continue
            bucket.append(self.get_track(track_id))
            present.add(track_id)

    def remove_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        drop = set(track_ids)
        bad = drop & self.fail_ids
        if bad:
            raise RuntimeError(f"simulated failure removing {bad}")
        bucket = self.playlists.get(playlist_id, [])
        self.playlists[playlist_id] = [t for t in bucket if t.id not in drop]

    def create_playlist(self, title: str, description: str = "", public: bool = False) -> Playlist:
        pid = f"pl{self._next_id}"
        self._next_id += 1
        self.playlists[pid] = []
        return Playlist(id=pid, title=title, service=self.service, description=description, public=public)

    def similar_tracks(self, track: Track, *, limit: int = 20) -> list[Track]:
        return list(self.catalog[:limit])
