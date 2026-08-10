"""The append-only event log — the database of record.

Everything else (artifact, anchor, node, edge, support) is a projection that
can be rebuilt by replay. ``actor`` separates human judgments from model
proposals; it is what makes the corpus trainable rather than circular.
"""

from __future__ import annotations

import json
import sqlite3

from inspeg.util import utcnow_iso

# Event kinds understood by the projection.
ARTIFACT_ADDED = "artifact_added"
ARTIFACT_REDACTED = "artifact_redacted"
ARTIFACT_DELETED = "artifact_deleted"  # hard delete; log keeps the tombstone (ADR 0010)
ARTIFACT_SOURCE_UPGRADED = "artifact_source_upgraded"  # tier-monotonic (ADR 0008)
ANCHOR_ADDED = "anchor_added"
NODE_ASSERTED = "node_asserted"
EDGE_ASSERTED = "edge_asserted"
EDGE_RETRACTED = "edge_retracted"
SUPPORT_ADDED = "support_added"
SUPPORT_REMOVED = "support_removed"  # label retraction (ADR 0006)


def append_event(conn: sqlite3.Connection, kind: str, payload: dict, actor: str = "human") -> int:
    """Append one immutable event; returns its sequence number."""
    cur = conn.execute(
        "INSERT INTO event (ts, kind, payload, actor) VALUES (?, ?, ?, ?)",
        (utcnow_iso(), kind, json.dumps(payload, ensure_ascii=False), actor),
    )
    return int(cur.lastrowid or 0)
