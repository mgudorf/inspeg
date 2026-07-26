"""Materialize the projection tables from events.

Never write artifact/anchor/node/edge/support directly: route every mutation
through ``Store.record`` so the log and the projection cannot diverge and
``replay`` can rebuild the projection from scratch after a schema change.

Event kinds the projection does not recognize are skipped deliberately — the
log is allowed to carry richer history than any given projection consumes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from inspeg.store import events as ev


def _pick(payload: dict, keys: tuple[str, ...]) -> dict:
    return {k: payload.get(k) for k in keys}


def _apply_artifact_added(conn: sqlite3.Connection, p: dict) -> None:
    row = _pick(
        p,
        (
            "id",
            "mimetype",
            "byte_len",
            "path",
            "captured_at",
            "provenance",
            "source_uri",
            "source_app",
            "derived_from",
            "derivation",
        ),
    )
    # Re-capturing identical content is a new event but the same artifact.
    conn.execute(
        """INSERT OR IGNORE INTO artifact
           (id, mimetype, byte_len, path, captured_at, provenance,
            source_uri, source_app, derived_from, derivation)
           VALUES (:id, :mimetype, :byte_len, :path, :captured_at, :provenance,
                   :source_uri, :source_app, :derived_from, :derivation)""",
        row,
    )


def _apply_artifact_redacted(conn: sqlite3.Connection, p: dict) -> None:
    # The blob file itself is deleted by the service (once, when the event is
    # first recorded); the projection only tracks the flag so replay stays
    # a pure DB operation.
    conn.execute("UPDATE artifact SET redacted = 1 WHERE id = ?", (p["id"],))


def _apply_anchor_added(conn: sqlite3.Connection, p: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO anchor (id, artifact_id, selector_type, selector)"
        " VALUES (?, ?, ?, ?)",
        (p["id"], p["artifact_id"], p["selector_type"], json.dumps(p["selector"], sort_keys=True)),
    )


def _apply_node_asserted(conn: sqlite3.Connection, p: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO node (id, label, props) VALUES (?, ?, ?)",
        (p["id"], p["label"], json.dumps(p.get("props") or {}, sort_keys=True)),
    )
    conn.execute(
        "INSERT OR IGNORE INTO node_alias (node_id, surface) VALUES (?, ?)",
        (p["id"], p["label"]),
    )


def _apply_edge_asserted(conn: sqlite3.Connection, p: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO edge (id, src, type, dst, props, valid_from, valid_to)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            p["id"],
            p["src"],
            p["type"],
            p["dst"],
            json.dumps(p.get("props") or {}, sort_keys=True),
            p.get("valid_from"),
            p.get("valid_to"),
        ),
    )


def _apply_support_added(conn: sqlite3.Connection, p: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO support (subject_kind, subject_id, anchor_id, role)"
        " VALUES (?, ?, ?, ?)",
        (p["subject_kind"], p["subject_id"], p["anchor_id"], p["role"]),
    )


_APPLIERS: dict[str, Callable[[sqlite3.Connection, dict], None]] = {
    ev.ARTIFACT_ADDED: _apply_artifact_added,
    ev.ARTIFACT_REDACTED: _apply_artifact_redacted,
    ev.ANCHOR_ADDED: _apply_anchor_added,
    ev.NODE_ASSERTED: _apply_node_asserted,
    ev.EDGE_ASSERTED: _apply_edge_asserted,
    ev.SUPPORT_ADDED: _apply_support_added,
}


def apply(conn: sqlite3.Connection, kind: str, payload: dict) -> None:
    applier = _APPLIERS.get(kind)
    if applier is not None:
        applier(conn, payload)


def replay(conn: sqlite3.Connection) -> int:
    """Rebuild the projection from the event log; returns events replayed.

    ``proposal`` is untouched: proposals are not event-sourced yet (M4), so
    clearing that table here would destroy data instead of rebuilding it.
    """
    # Children before parents, for foreign keys.
    for table in ("support", "edge", "node_alias", "node", "anchor", "artifact"):
        conn.execute(f"DELETE FROM {table}")
    count = 0
    for row in conn.execute("SELECT kind, payload FROM event ORDER BY seq"):
        apply(conn, row["kind"], json.loads(row["payload"]))
        count += 1
    return count
