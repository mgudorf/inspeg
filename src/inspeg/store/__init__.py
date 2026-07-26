"""Storage façade: blob store + event log + projection behind one lock."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from inspeg.store import projection
from inspeg.store.blobstore import BlobStore
from inspeg.store.db import open_db
from inspeg.store.events import append_event


class Store:
    """Owns the SQLite database and the blob directory for one data dir.

    A single process owns the store; a re-entrant lock serializes access so
    the API threadpool and the hotkey thread can share it safely.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blobs = BlobStore(self.data_dir)
        self.conn = open_db(self.data_dir / "inspeg.db")
        self._lock = threading.RLock()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialize writers; commit on success, roll back on error."""
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def record(self, kind: str, payload: dict, actor: str = "human") -> int:
        """Append an event and apply it to the projection. Call inside tx()."""
        with self._lock:
            seq = append_event(self.conn, kind, payload, actor)
            projection.apply(self.conn, kind, payload)
            return seq

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def replay(self) -> int:
        """Rebuild the projection from the event log."""
        with self.tx():
            return projection.replay(self.conn)

    def close(self) -> None:
        self.conn.close()
