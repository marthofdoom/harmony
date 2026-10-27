"""Shared test fixtures: a duck-typed fake provider, and the host-isolation guard.

``harmony.providers.base.MusicProvider`` is owned by another module and may
not exist yet at import time, so this fake implements the ABC's surface by
duck typing only — it is never imported as a subclass of anything from
``harmony.providers``.

Host isolation: no test may read or write the developer's real Harmony profile
(``~/.config/harmony``, ``~/.local/share/harmony``, ``~/.cache/harmony``) or
real keyring. A test that builds an unpatched ``Settings``/``AppState``/
``Engine`` and saves (e.g. the Qobuz app-id scrape persisting its result) used
to land in the real ``settings.json``. The autouse fixture below points every
XDG dir and ``$HOME`` at a per-test temp dir and forces the keyring's null
backend; ``harmony.config`` resolves its dirs lazily (``platformdirs`` reads
the environment on every call), so this takes effect for code that is already
imported. Tests that want specific paths still monkeypatch ``settings_path``
etc. on top of this.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from harmony.models import Playlist, SearchResults, Service, Track

_ISOLATED_ENV = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "HOME")
_FAIL_KEYRING = "keyring.backends.fail.Keyring"


def _point_at(root: Path) -> dict[str, str]:
    env = {
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_RUNTIME_DIR": str(root / "run"),
        "PYTHON_KEYRING_BACKEND": _FAIL_KEYRING,
    }
    for key, value in env.items():
        os.environ[key] = value
        if key != "PYTHON_KEYRING_BACKEND":
            Path(value).mkdir(parents=True, exist_ok=True)
    return env


# Import-time guard: anything that touches a path while pytest is still
# collecting (module-level code, session fixtures) is isolated too.
_SESSION_ROOT = Path(tempfile.mkdtemp(prefix="harmony-tests-"))
atexit.register(shutil.rmtree, _SESSION_ROOT, True)
_point_at(_SESSION_ROOT)


@pytest.fixture(autouse=True)
def _isolate_host(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Per-test profile dirs + a keyring that stores nothing."""
    root = tmp_path_factory.mktemp("host")
    for name, value in _point_at(root).items():
        monkeypatch.setenv(name, value)  # restores the previous value afterwards
    for name in _ISOLATED_ENV:
        assert str(root) in os.environ[name]
    try:  # the env var only matters the first time keyring picks a backend; pin it explicitly too
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except ImportError:
        yield
        return
    previous = keyring.get_keyring()
    keyring.set_keyring(FailKeyring())
    yield
    keyring.set_keyring(previous)


class FakeProvider:
    """A minimal in-memory stand-in for a real MusicProvider.

    ``search`` ignores the query text and just returns the catalog (capped
    to ``limit``) — tests control relevance by choosing what goes in the
    catalog, not by exercising a search index.

    By default ``add_tracks``/``remove_tracks`` process the whole id list as
    one internal step and ``add_tracks`` silently dedupes against what's
    already in the playlist — good enough for tests that don't care about
    provider-internal batching. The real providers are neither: both chunk
    internally (YT 100, Qobuz 50 — see ``providers/ytmusic.py`` /
    ``providers/qobuz.py``) and are *not* transactional, so a mid-list
    failure leaves earlier chunks already written; and a real ``add_tracks``
    just appends, so calling it twice with the same id creates a literal
    duplicate row. Pass ``add_chunk_size`` and ``idempotent_add=False`` to
    opt into that harsher, more realistic behaviour for tests that need to
    verify code which must survive it (e.g. sync.py's batch-failure
    fallback). Both default to the old forgiving behaviour so existing
    fixtures/tests are unaffected.
    """

    def __init__(
        self,
        service: Service,
        catalog: list[Track] | None = None,
        *,
        add_chunk_size: int | None = None,
        idempotent_add: bool = True,
    ) -> None:
        self.service = service
        self.catalog: list[Track] = list(catalog or [])
        self.playlists: dict[str, list[Track]] = {}
        self.fail_ids: set[str] = set()
        self.add_chunk_size = add_chunk_size
        self.idempotent_add = idempotent_add
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

    def _chunks(self, track_ids: list[str]) -> list[list[str]]:
        size = self.add_chunk_size
        if not size:
            return [list(track_ids)]
        return [list(track_ids[i : i + size]) for i in range(0, len(track_ids), size)]

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        """Write ``track_ids``, chunked like a real provider.

        Each chunk is atomic — either the whole chunk lands or none of it
        does, mirroring one HTTP request per chunk — but chunks are not
        transactional with each other: once a chunk succeeds it stays
        written even if a later chunk in the same call raises. With
        ``idempotent_add=False`` (the real-provider-accurate setting), a
        track id that is added twice produces two rows, matching a real
        playlist append endpoint rather than a set.
        """
        bucket = self.playlists.setdefault(playlist_id, [])
        present = {t.id for t in bucket}
        for chunk in self._chunks(list(track_ids)):
            bad = [tid for tid in chunk if tid in self.fail_ids]
            if bad:
                raise RuntimeError(f"simulated failure adding chunk containing {bad}")
            for track_id in chunk:
                if self.idempotent_add and track_id in present:
                    continue
                bucket.append(self.get_track(track_id))
                present.add(track_id)

    def remove_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        """Remove ``track_ids``, chunked like a real provider (see ``add_tracks``)."""
        for chunk in self._chunks(list(track_ids)):
            bad = [tid for tid in chunk if tid in self.fail_ids]
            if bad:
                raise RuntimeError(f"simulated failure removing chunk containing {bad}")
            drop = set(chunk)
            bucket = self.playlists.get(playlist_id, [])
            self.playlists[playlist_id] = [t for t in bucket if t.id not in drop]

    def create_playlist(self, title: str, description: str = "", public: bool = False) -> Playlist:
        pid = f"pl{self._next_id}"
        self._next_id += 1
        self.playlists[pid] = []
        return Playlist(id=pid, title=title, service=self.service, description=description, public=public)

    def similar_tracks(self, track: Track, *, limit: int = 20) -> list[Track]:
        return list(self.catalog[:limit])
