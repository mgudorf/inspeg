"""Full-text search over captured text — an EPHEMERAL cache, never a
migration.

Full text lives in blobs, not events, so populating FTS during replay would
break "replay is a pure DB operation" and cost O(blob bytes). Instead:
``<data-dir>/cache.db`` (own connection, own ``user_version``, deletable at
any time) holds an FTS5 table fed by a post-commit subscriber. It is a
second content-bearing file on disk — listed as such in SECURITY.md — and
redaction MUST reach it: row deletion happens synchronously inside the
commit callback (before ``redact_artifact`` returns), and the startup
reconcile purges rows for redacted artifacts as a crash backstop (ADR 0002).
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import sqlite3
import threading
from pathlib import Path

from inspeg.store import Store
from inspeg.store import events as ev

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_STOP = object()


class FtsIndex:
    """FTS5 index over text-blob artifacts.

    Deletions (redactions) run synchronously in the post-commit callback —
    single-row, fast, and ordering-critical. Insertions read blobs, so they
    are queued to a worker thread (never block a capture); tests construct
    with ``start_worker=False`` and drain with ``process_pending()``.
    """

    def __init__(self, store: Store, path: Path, *, start_worker: bool = True) -> None:
        self.store = store
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._ensure_schema()
        self._reconcile_redactions()
        self._worker: threading.Thread | None = None
        if start_worker:
            self._worker = threading.Thread(
                target=self._worker_loop, name="inspeg-fts", daemon=True
            )
            self._worker.start()

    def _ensure_schema(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            with self._lock:
                self.conn.execute("DROP TABLE IF EXISTS artifact_fts")
                self.conn.execute(
                    "CREATE VIRTUAL TABLE artifact_fts USING fts5(artifact_id UNINDEXED, text)"
                )
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self.conn.commit()

    def _reconcile_redactions(self) -> None:
        """Crash backstop: a redaction or delete that died between the store
        commit and our delete must not leave 'destroyed' content searchable.
        A deleted artifact has no row at all, so reconcile by keep-set: any
        indexed id that is not a live, unredacted blob is purged."""
        live = {
            row["id"]
            for row in self.store.read_query(
                "SELECT id FROM artifact WHERE kind = 'blob' AND redacted = 0"
            )
        }
        with self._lock:
            indexed = [
                row["artifact_id"]
                for row in self.conn.execute("SELECT DISTINCT artifact_id FROM artifact_fts")
            ]
            stale = [(artifact_id,) for artifact_id in indexed if artifact_id not in live]
            if stale:
                self.conn.executemany("DELETE FROM artifact_fts WHERE artifact_id = ?", stale)
                self.conn.commit()

    # ── post-commit subscriber ──────────────────────────────────────────────

    def on_commit(self, events: list[dict]) -> None:
        for event in events:
            if event["kind"] in (ev.ARTIFACT_REDACTED, ev.ARTIFACT_DELETED):
                # Synchronous: the "content destroyed" promise (ADR 0002 for
                # redaction, ADR 0010 for delete) must hold for every copy
                # before the service call returns.
                payload = self.store.read_query_one(
                    "SELECT payload FROM event WHERE seq = ?", (event["seq"],)
                )
                if payload is not None:
                    self.delete(json.loads(payload["payload"])["id"])
            elif event["kind"] == ev.ARTIFACT_ADDED:
                self._queue.put(event["seq"])

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            try:
                self._index_from_event(item)
            except Exception:
                log.debug("fts index update failed", exc_info=True)

    def process_pending(self) -> None:
        """Drain the insert queue on the calling thread (tests, reindex)."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _STOP:
                return
            with contextlib.suppress(Exception):
                self._index_from_event(item)

    def _index_from_event(self, seq: int) -> None:
        row = self.store.read_query_one("SELECT payload FROM event WHERE seq = ?", (seq,))
        if row is None:
            return
        self.index_artifact(json.loads(row["payload"])["id"])

    # ── index operations ────────────────────────────────────────────────────

    def index_artifact(self, artifact_id: str) -> None:
        row = self.store.read_query_one(
            "SELECT id, kind, mimetype, redacted FROM artifact WHERE id = ?", (artifact_id,)
        )
        if (
            row is None
            or row["redacted"]
            or row["kind"] != "blob"
            or not row["mimetype"].startswith("text/")
        ):
            return
        try:
            text = self.store.blobs.get(artifact_id).decode("utf-8", "replace")
        except (FileNotFoundError, ValueError):
            return
        with self._lock:
            self.conn.execute("DELETE FROM artifact_fts WHERE artifact_id = ?", (artifact_id,))
            self.conn.execute(
                "INSERT INTO artifact_fts (artifact_id, text) VALUES (?, ?)",
                (artifact_id, text),
            )
            self.conn.commit()

    def delete(self, artifact_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM artifact_fts WHERE artifact_id = ?", (artifact_id,))
            self.conn.commit()

    def rebuild(self) -> int:
        """Full reindex from the projection (``inspeg reindex``). Redacted
        artifacts are skipped by ``index_artifact``."""
        with self._lock:
            self.conn.execute("DELETE FROM artifact_fts")
            self.conn.commit()
        count = 0
        for row in self.store.read_query(
            "SELECT id FROM artifact WHERE kind = 'blob' AND redacted = 0"
            " AND mimetype LIKE 'text/%'"
        ):
            self.index_artifact(row["id"])
            count += 1
        return count

    def search(self, query: str, *, limit: int = 25) -> list[dict]:
        limit = max(1, min(limit, 100))
        try:
            with self._lock:
                rows = self.conn.execute(
                    """SELECT artifact_id,
                              snippet(artifact_fts, 1, '<<', '>>', ' … ', 12) AS snippet
                       FROM artifact_fts WHERE artifact_fts MATCH ?
                       ORDER BY rank LIMIT ?""",
                    (query, limit),
                ).fetchall()
        except sqlite3.OperationalError as exc:  # malformed FTS query syntax
            raise ValueError(f"bad search query: {exc}") from exc
        return [{"artifact_id": row["artifact_id"], "snippet": row["snippet"]} for row in rows]

    def close(self) -> None:
        if self._worker is not None:
            self._queue.put(_STOP)
            self._worker.join(timeout=2)
        self.conn.close()
