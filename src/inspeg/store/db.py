"""SQLite connection setup and the migration runner.

Migrations are numbered ``.sql`` files in the repo-level ``schema/`` directory.
Applied migrations are tracked by filename and never edited — schema evolution
happens by adding a new file (and, when the projection shape changes, replaying
the event log).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from inspeg.util import resource_dir, utcnow_iso


def open_db(path: Path) -> sqlite3.Connection:
    """Connect with the standard pragmas. Migrations are applied by the Store,
    which replays the projection whenever a new migration lands."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def open_db_readonly(path: Path) -> sqlite3.Connection:
    """A second, read-only connection for GET handlers.

    WAL gives readers snapshot isolation, so HUD/extension reads never contend
    with the capture worker on the Store's write lock. ``mode=ro`` makes the
    read-only claim structural: this connection *cannot* write, so the daemon
    stays a single-writer system by construction.
    """
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 2000")
    return conn


def apply_migrations(conn: sqlite3.Connection, schema_dir: Path | None = None) -> list[str]:
    schema_dir = schema_dir or resource_dir("schema")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration"
        " (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row["name"] for row in conn.execute("SELECT name FROM schema_migration")}
    ran: list[str] = []
    for sql_file in sorted(schema_dir.glob("*.sql")):
        if sql_file.name in applied:
            continue
        conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migration (name, applied_at) VALUES (?, ?)",
            (sql_file.name, utcnow_iso()),
        )
        ran.append(sql_file.name)
    conn.commit()
    return ran
