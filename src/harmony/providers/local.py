"""``LocalLibraryProvider`` — the instance's own music folders as a service.

Wraps :class:`harmony.library.LibraryIndex` in the ``MusicProvider`` contract so
the engine treats library tracks like any other service: search, album/artist
pages, queue and streaming (``resolve_stream`` hands back a ``file://`` source
that the web stream proxy and the cast relay serve from disk). Read-only: no
playlists, likes or recommendations — those live on the streaming services.

Ids are stable hashes (path for tracks, normalised album-artist + album for
albums, name for artists) so a rescan or a Lidarr re-import keeps queue entries
and links valid.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..errors import NotSupportedError, ProviderError
from ..library import ArtSigner, LibraryIndex
from ..models import Album, Artist, Playlist, SearchResults, Service, StreamSource, Track
from .base import MusicProvider

_LABELS = {"audio/flac": "FLAC", "audio/mpeg": "MP3", "audio/mp4": "AAC/ALAC",
           "audio/aac": "AAC", "audio/ogg": "Ogg", "audio/wav": "WAV", "audio/aiff": "AIFF"}


class LocalLibraryProvider(MusicProvider):
    service = Service.LOCAL

    def __init__(self, index: LibraryIndex, signer: ArtSigner) -> None:
        self.index = index
        self.signer = signer

    # -- auth (nothing to sign in to) --------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return True

    def authenticate(self) -> None:
        return None

    def account_name(self) -> str | None:
        n = self.index.stats()["tracks"]
        return f"{n} track{'s' if n != 1 else ''}"

    # -- row → model -------------------------------------------------------

    def _art(self, album_id: str) -> str:
        return self.signer.path_for(album_id)

    def _track(self, r: dict[str, Any]) -> Track:
        return Track(
            id=r["id"], title=r["title"], service=Service.LOCAL,
            artists=[r["artist"]], artist_ids=[r["artist_id"]],
            album=r["album"], album_id=r["album_id"], duration_s=r["duration_s"],
            isrc=r["isrc"], year=r["year"], track_number=r["track_no"],
            artwork_url=self._art(r["album_id"]),
            raw={"path": r["path"], "mime": r["mime"], "album_artist": r["album_artist"],
                 "mb_releasegroup": r["mb_releasegroup"]},
        )

    def _album(self, r: dict[str, Any]) -> Album:
        return Album(
            id=r["album_id"], title=r["album"], service=Service.LOCAL,
            artists=[r["album_artist"]], artist_ids=[r["album_artist_id"]],
            year=r["year"], date=r["date"], track_count=r["track_count"],
            artwork_url=self._art(r["album_id"]),
            raw={"mb_releasegroup": r.get("mb_releasegroup"), "added_at": r.get("added_at")},
        )

    # -- reads ---------------------------------------------------------------

    def search(self, query: str, *, kinds: Sequence[str] = ("tracks",),
               limit: int = 25) -> SearchResults:
        r = self.index.search(query, kinds=kinds, limit=limit)
        return SearchResults(
            tracks=[self._track(t) for t in r["tracks"]],
            albums=[self._album(a) for a in r["albums"]],
            artists=[Artist(id=a["id"], name=a["name"], service=Service.LOCAL)
                     for a in r["artists"]],
        )

    def get_track(self, track_id: str) -> Track:
        row = self.index.track(track_id)
        if row is None:
            raise ProviderError(f"Track {track_id} isn't in the library.")
        return self._track(row)

    def get_album_detail(self, album_id: str) -> Album:
        row = self.index.album(album_id)
        if row is None:
            raise ProviderError(f"Album {album_id} isn't in the library.")
        return self._album(row)

    def get_album_tracks(self, album_id: str) -> list[Track]:
        return [self._track(r) for r in self.index.album_tracks(album_id)]

    def get_artist_detail(self, artist_id: str) -> Artist:
        row = self.index.artist(artist_id)
        if row is None:
            raise ProviderError(f"Artist {artist_id} isn't in the library.")
        return Artist(id=artist_id, name=row["name"], service=Service.LOCAL)

    def get_artist_albums(self, artist_id: str, *, limit: int = 100) -> list[Album]:
        return [self._album(a) for a in self.index.albums(artist_id=artist_id, limit=limit)]

    def get_artist_top_tracks(self, artist_id: str, *, limit: int = 20) -> list[Track]:
        return [self._track(r) for r in self.index.artist_tracks(artist_id, limit=limit)]

    def list_albums(self, *, order: str = "recent", limit: int = 60,
                    offset: int = 0) -> list[Album]:
        """Browse the library (not part of the provider contract)."""
        return [self._album(a) for a in self.index.albums(order=order, limit=limit,
                                                          offset=offset)]

    def list_artists(self, *, limit: int = 2000) -> list[dict[str, Any]]:
        return self.index.artists(limit=limit)

    # -- streaming -----------------------------------------------------------

    def resolve_stream(self, track_id: str, *, max_quality: bool = False) -> StreamSource:
        row = self.index.track(track_id)
        if row is None:
            raise ProviderError(f"Track {track_id} isn't in the library.")
        path = Path(row["path"])
        if not path.is_file():
            raise ProviderError(f"“{row['title']}” is missing from disk (rescan the library).")
        return StreamSource(url=path.as_uri(), mime_type=row["mime"],
                            container=path.suffix.lower().lstrip(".") or None,
                            label=f"Library · {_LABELS.get(row['mime'], path.suffix.upper()[1:])}")

    # -- playlists / social: not a thing for a folder of files -----------------

    def list_playlists(self) -> list[Playlist]:
        return []

    def get_playlist(self, playlist_id: str) -> Playlist:
        raise NotSupportedError("The local library has no playlists.")

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        raise NotSupportedError("The local library has no playlists.")

    def create_playlist(self, title: str, description: str = "", public: bool = False) -> Playlist:
        raise NotSupportedError("Playlists live on a streaming service, not the local library.")

    def add_tracks(self, playlist_id: str, track_ids: Sequence[str]) -> None:
        raise NotSupportedError("The local library has no playlists.")

    def remove_tracks(self, playlist_id: str, track_ids: Sequence[str]) -> None:
        raise NotSupportedError("The local library has no playlists.")

    def delete_playlist(self, playlist_id: str) -> None:
        raise NotSupportedError("The local library has no playlists.")

    def rename_playlist(self, playlist_id: str, title: str, description: str | None = None) -> None:
        raise NotSupportedError("The local library has no playlists.")

    def similar_tracks(self, track: Track, *, limit: int = 20) -> list[Track]:
        return []

    def liked_tracks(self, *, limit: int = 500) -> list[Track]:
        return []


__all__ = ["LocalLibraryProvider"]
