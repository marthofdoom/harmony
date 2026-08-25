"""Local SQLite persistence: cross-service track links, playlist pairings,
playlist snapshots, and a generic TTL cache.

A single connection is shared across threads (``check_same_thread=False``)
because provider calls happen on a worker thread while the UI may read the
db on the main thread for display. Every public method therefore takes the
``_lock`` for the duration of its statement(s); sqlite's own locking is not
enough on its own because we sometimes need multiple statements (e.g. the
symmetric writes in ``put_link``) to be atomic with respect to other
threads.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import config
from .models import Service

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS track_links (
    src_service TEXT NOT NULL,
    src_id      TEXT NOT NULL,
    dst_service TEXT NOT NULL,
    dst_id      TEXT NOT NULL,
    score       REAL NOT NULL,
    confidence  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (src_service, src_id, dst_service)
);

CREATE TABLE IF NOT EXISTS playlist_links (
    local_id    TEXT PRIMARY KEY,
    ytmusic_id  TEXT,
    qobuz_id    TEXT,
    title       TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service     TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    taken_at    REAL NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class Database:
    """Thread-safe wrapper around a single sqlite3 connection.

    ``path`` defaults to the standard data dir but tests pass ``":memory:"``
    or a tmp file so the whole suite never touches the user's real db.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            path = config.data_dir() / "harmony.db"
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # -- lifecycle ----------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- migration ------------------------------------------------------

    def _migrate(self) -> None:
        """Create the schema idempotently and record the version in ``kv``.

        Future schema changes hook in here by branching on the stored
        version and applying incremental ALTERs before bumping it — nothing
        downstream needs to know a migration happened.
        """
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value_json FROM kv WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO kv (key, value_json, updated_at) VALUES "
                    "('schema_version', ?, ?)",
                    (json.dumps(SCHEMA_VERSION), time.time()),
                )
            self._conn.commit()

    # -- track_links ------------------------------------------------------

    def put_link(
        self,
        src_service: Service,
        src_id: str,
        dst_service: Service,
        dst_id: str,
        score: float,
        confidence: str,
    ) -> None:
        """Record a match in both directions so a lookup from either side hits.

        Two rows, sharing the ``(src_service, src_id, dst_service)`` PK
        family, are written in one transaction so a concurrent reader never
        observes only one half of the pair.
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO track_links "
                "(src_service, src_id, dst_service, dst_id, score, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (src_service, src_id, dst_service) DO UPDATE SET "
                "dst_id=excluded.dst_id, score=excluded.score, "
                "confidence=excluded.confidence, created_at=excluded.created_at",
                (src_service.value, src_id, dst_service.value, dst_id, score, confidence, now),
            )
            self._conn.execute(
                "INSERT INTO track_links "
                "(src_service, src_id, dst_service, dst_id, score, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (src_service, src_id, dst_service) DO UPDATE SET "
                "dst_id=excluded.dst_id, score=excluded.score, "
                "confidence=excluded.confidence, created_at=excluded.created_at",
                (dst_service.value, dst_id, src_service.value, src_id, score, confidence, now),
            )
            self._conn.commit()

    def get_link(
        self, src_service: Service, src_id: str, dst_service: Service
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM track_links WHERE src_service=? AND src_id=? AND dst_service=?",
                (src_service.value, src_id, dst_service.value),
            ).fetchone()
        return dict(row) if row is not None else None

    def forget_link(self, src_service: Service, src_id: str, dst_service: Service) -> None:
        """Drop both halves of a link (best-effort; the reverse row may not exist)."""
        with self._lock:
            link = self._conn.execute(
                "SELECT dst_id FROM track_links WHERE src_service=? AND src_id=? "
                "AND dst_service=?",
                (src_service.value, src_id, dst_service.value),
            ).fetchone()
            self._conn.execute(
                "DELETE FROM track_links WHERE src_service=? AND src_id=? AND dst_service=?",
                (src_service.value, src_id, dst_service.value),
            )
            if link is not None:
                self._conn.execute(
                    "DELETE FROM track_links WHERE src_service=? AND src_id=? "
                    "AND dst_service=?",
                    (dst_service.value, link["dst_id"], src_service.value),
                )
            self._conn.commit()

    # -- playlist_links ------------------------------------------------------

    def link_playlists(
        self,
        local_id: str,
        title: str,
        *,
        ytmusic_id: str | None = None,
        qobuz_id: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO playlist_links (local_id, ytmusic_id, qobuz_id, title, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (local_id) DO UPDATE SET "
                "ytmusic_id=COALESCE(excluded.ytmusic_id, playlist_links.ytmusic_id), "
                "qobuz_id=COALESCE(excluded.qobuz_id, playlist_links.qobuz_id), "
                "title=excluded.title",
                (local_id, ytmusic_id, qobuz_id, title, time.time()),
            )
            self._conn.commit()

    def get_playlist_link(self, local_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM playlist_links WHERE local_id=?", (local_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_playlist_links(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM playlist_links").fetchall()
        return [dict(r) for r in rows]

    def unlink_playlists(self, local_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM playlist_links WHERE local_id=?", (local_id,))
            self._conn.commit()

    # -- snapshots ------------------------------------------------------

    def save_snapshot(self, service: Service, playlist_id: str, payload: Any) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO snapshots (service, playlist_id, taken_at, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (service.value, playlist_id, time.time(), json.dumps(payload)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_snapshots(self, service: Service, playlist_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, service, playlist_id, taken_at FROM snapshots "
                "WHERE service=? AND playlist_id=? ORDER BY taken_at DESC",
                (service.value, playlist_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_snapshot(self, snapshot_id: int) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    # -- kv / cache ------------------------------------------------------

    def cache_put(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET "
                "value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, json.dumps(value), time.time()),
            )
            self._conn.commit()

    def cache_get(self, key: str, max_age_s: float) -> Any | None:
        """Return the cached value, or None if absent or older than ``max_age_s``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json, updated_at FROM kv WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        if time.time() - row["updated_at"] > max_age_s:
            return None
        return json.loads(row["value_json"])
