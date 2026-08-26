"""``MusicProvider`` — the service-neutral contract every backend implements.

Concrete providers (``ytmusic.py``, ``qobuz.py``) translate their own raw API
payloads into ``harmony.models`` types so the rest of the app (matching, sync,
UI) never has to branch on which service a row came from. See
``docs/ARCHITECTURE.md`` for the full contract.
"""

from __future__ import annotations

import functools
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from typing import Any, TypeVar

from ..errors import NotSupportedError, RateLimitedError
from ..models import Album, Playlist, SearchResults, Service, StreamSource, Track

log = logging.getLogger(__name__)

T = TypeVar("T")


def _chunked(seq: Sequence[T], n: int) -> Iterator[list[T]]:
    """Yield ``seq`` in consecutive slices of at most ``n`` items.

    Shared by both providers' ``add_tracks``/``remove_tracks`` since each
    backend caps how many items a single write request may carry.
    """
    for i in range(0, len(seq), n):
        yield list(seq[i : i + n])


def retry_on_rate_limit(attempts: int = 3, base_delay: float = 1.0) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Retry a call up to ``attempts`` times (exponential backoff) on ``RateLimitedError``.

    Honours a server-provided ``retry_after`` when the exception carries one;
    otherwise backs off ``base_delay * 2**attempt`` seconds. Re-raises the last
    error once attempts are exhausted so callers still see a ``RateLimitedError``.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: RateLimitedError | None = None
            for attempt in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except RateLimitedError as exc:
                    last_exc = exc
                    if attempt == attempts - 1:
                        break
                    delay = exc.retry_after if exc.retry_after is not None else base_delay * (2**attempt)
                    log.warning(
                        "%s rate limited (attempt %d/%d); retrying in %.1fs",
                        fn.__qualname__,
                        attempt + 1,
                        attempts,
                        delay,
                    )
                    time.sleep(delay)
            assert last_exc is not None  # attempts >= 1 guarantees this is set
            raise last_exc

        return wrapper

    return decorator


class MusicProvider(ABC):
    """Blocking, single-worker-thread-at-a-time façade over one backend.

    Callers must run these methods off the GTK main loop via
    ``harmony.tasks.run_async`` — see the threading rule in
    ``docs/ARCHITECTURE.md``.
    """

    service: Service

    @property
    @abstractmethod
    def is_authenticated(self) -> bool: ...

    @abstractmethod
    def authenticate(self) -> None:
        """Establish or refresh credentials. Raises ``AuthError`` on failure."""

    @abstractmethod
    def account_name(self) -> str | None: ...

    @abstractmethod
    def search(
        self, query: str, *, kinds: Sequence[str] = ("tracks",), limit: int = 25
    ) -> SearchResults:
        """``kinds`` is a subset of ``{"tracks","albums","artists","playlists"}``."""

    @abstractmethod
    def get_track(self, track_id: str) -> Track: ...

    @abstractmethod
    def get_album_tracks(self, album_id: str) -> list[Track]: ...

    @abstractmethod
    def get_artist_albums(self, artist_id: str, *, limit: int = 100) -> list[Album]: ...

    @abstractmethod
    def get_artist_top_tracks(self, artist_id: str, *, limit: int = 20) -> list[Track]: ...

    @abstractmethod
    def list_playlists(self) -> list[Playlist]: ...

    @abstractmethod
    def get_playlist(self, playlist_id: str) -> Playlist: ...

    @abstractmethod
    def get_playlist_tracks(self, playlist_id: str) -> list[Track]: ...

    @abstractmethod
    def create_playlist(
        self, title: str, description: str = "", public: bool = False
    ) -> Playlist: ...

    @abstractmethod
    def add_tracks(self, playlist_id: str, track_ids: Sequence[str]) -> None: ...

    @abstractmethod
    def remove_tracks(self, playlist_id: str, track_ids: Sequence[str]) -> None: ...

    @abstractmethod
    def delete_playlist(self, playlist_id: str) -> None: ...

    @abstractmethod
    def rename_playlist(
        self, playlist_id: str, title: str, description: str | None = None
    ) -> None: ...

    @abstractmethod
    def similar_tracks(self, track: Track, *, limit: int = 20) -> list[Track]: ...

    @abstractmethod
    def liked_tracks(self, *, limit: int = 500) -> list[Track]: ...

    def resolve_stream(self, track_id: str) -> StreamSource:
        """Resolve a directly-fetchable, device-friendly audio stream for a track.

        Returns a time-limited provider URL plus the container/mime and any headers
        the CDN fetch needs. Consumed by the playback relay. Providers that can't
        stream raise NotSupportedError.
        """
        raise NotSupportedError(f"{type(self).__name__} does not support stream resolution")


__all__ = ["MusicProvider", "_chunked", "retry_on_rate_limit"]
