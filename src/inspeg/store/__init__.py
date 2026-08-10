"""Storage façade: blob store + event log + projection behind one lock."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from inspeg.store import projection
from inspeg.store.blobstore import BlobStore
from inspeg.store.db import apply_migrations, open_db, open_db_readonly
from inspeg.store.events import append_event


class StoreLockedError(RuntimeError):
    """Another process already owns this data directory."""


def _lock_data_dir(path: Path) -> int:
    """Take an exclusive advisory lock on the data dir's lock file.

    The in-process RLock only serializes threads; this keeps a second daemon
    (different port, same ``--data-dir``) from racing the blobstore and the
    projection. The OS releases the lock if the process dies, so there are no
    stale locks to clean up.
    """
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise StoreLockedError(
            f"another inspeg process is already using {path.parent} (one instance per data dir)"
        ) from exc
    return fd


class Store:
    """Owns the SQLite database and the blob directory for one data dir.

    A single process owns the store; a re-entrant lock serializes access so
    the API threadpool and the hotkey thread can share it safely. A file lock
    on the data dir enforces the single-process claim across processes.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock_fd: int | None = _lock_data_dir(self.data_dir / ".lock")
        self.blobs = BlobStore(self.data_dir)
        self.conn = open_db(self.data_dir / "inspeg.db")
        self._lock = threading.RLock()
        self._tx_depth = 0
        # Post-commit subscribers (the SSE bridge). Fired only after a
        # successful outermost commit, so subscribers never see rolled-back
        # writes. Callbacks must be fast and must not raise.
        self._pending_events: list[dict] = []
        self.on_commit: list[Callable[[list[dict]], None]] = []
        # A new migration on an existing log means the projection's shape or
        # semantics changed: rebuild it from the events (the §3.2 insurance
        # policy — "replay, don't migrate").
        migrations_ran = apply_migrations(self.conn)
        if migrations_ran and self.query_one("SELECT 1 FROM event LIMIT 1") is not None:
            self.replay()
        self._sweep_stale_files()
        self.read_conn = open_db_readonly(self.data_dir / "inspeg.db")

    def _sweep_stale_files(self) -> None:
        """Drop crash leftovers: half-written ``.tmp`` files always; blobs no
        artifact references (rolled-back captures, redacted content) only when
        the event log is non-empty — an empty log next to existing blobs means
        the database is not the one that produced them, and deleting would be
        data loss, not cleanup.
        """
        if self.query_one("SELECT 1 FROM event LIMIT 1") is None:
            self.blobs.sweep(None)
            return
        # Pointer artifacts have no blob (path IS NULL, ADR 0005) — they must
        # neither contribute to nor be threatened by the sweep.
        keep = {
            row["path"]
            for row in self.query(
                "SELECT path FROM artifact WHERE redacted = 0 AND path IS NOT NULL"
            )
        }
        self.blobs.sweep(keep)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialize writers; commit on success, roll back on error.

        Re-entrant: only the outermost level commits/rolls back, and only a
        successful outermost commit fires the post-commit subscribers.
        """
        with self._lock:
            self._tx_depth += 1
            try:
                yield self.conn
                if self._tx_depth == 1:
                    self.conn.commit()
                    pending, self._pending_events = self._pending_events, []
                    for callback in list(self.on_commit):
                        # Subscribers must never break a committed write.
                        with contextlib.suppress(Exception):
                            callback(pending)
            except BaseException:
                if self._tx_depth == 1:
                    self.conn.rollback()
                    self._pending_events.clear()
                raise
            finally:
                self._tx_depth -= 1

    def record(self, kind: str, payload: dict, actor: str = "human") -> int:
        """Append an event and apply it to the projection. Call inside tx()."""
        with self._lock:
            seq = append_event(self.conn, kind, payload, actor)
            projection.apply(self.conn, kind, payload)
            self._pending_events.append({"seq": seq, "kind": kind})
            return seq

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def read_query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """Snapshot read on the dedicated read-only connection — never takes
        the write lock, so GET handlers cannot stall a capture."""
        return self.read_conn.execute(sql, params).fetchall()

    def read_query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.read_query(sql, params)
        return rows[0] if rows else None

    def replay(self) -> int:
        """Rebuild the projection from the event log."""
        with self.tx():
            return projection.replay(self.conn)

    def close(self) -> None:
        """Release the connections and the data-dir lock. Idempotent — the
        console-close handler and the normal shutdown path may both call it."""
        self.conn.close()
        self.read_conn.close()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
