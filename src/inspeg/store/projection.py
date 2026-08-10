"""Materialize the projection tables from events.

Never write artifact/anchor/node/edge/support directly: route every mutation
through ``Store.record`` so the log and the projection cannot diverge and
``replay`` can rebuild the projection from scratch after a schema change.

Event kinds the projection does not recognize are skipped deliberately — the
log is allowed to carry richer history than any given projection consumes.
The same rule applies *within* known kinds: appliers are total. They derive,
tolerate, and skip; they never raise, because replay must survive both older
events (pre-context payloads) and newer ones (roles or fields this binary has
never heard of). Validation that rejects lives in the service layer.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from inspeg.store import events as ev
from inspeg.util import (
    derive_context_key,
    normalize_predicate_label,
    normalize_source_uri,
    split_source_app,
)

# What THIS binary's schema accepts. Anything outside is a future shape:
# skipped, never raised on (ADR 0006).
_SUPPORT_SUBJECT_KINDS = frozenset({"node", "edge"})
_SUPPORT_ROLES = frozenset({"evidence", "commentary", "counterexample", "label"})

# Lower rank = better provenance. Unknown tiers rank worst so an upgrade to
# an unrecognized tier can never clobber a known one.
_PROVENANCE_RANK = {"exact": 1, "sourced": 2, "attributed": 3, "orphan": 4}


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
    # New-payload fields, with best-effort derivation for legacy events
    # (ADR 0008: the projection normalizes mechanically and never raises).
    row["kind"] = p.get("kind") or "blob"
    locator = p.get("locator")
    row["locator"] = json.dumps(locator, sort_keys=True) if locator else None
    exe, title = p.get("source_exe"), p.get("source_title")
    if exe is None and title is None:
        exe, title = split_source_app(p.get("source_app"))
    row["source_exe"], row["source_title"] = exe, title
    uri_norm = p.get("source_uri_norm")
    if uri_norm is None:
        uri_norm = normalize_source_uri(p.get("source_uri"))
    row["source_uri_norm"] = uri_norm
    context_key = p.get("context_key")
    if context_key is None:
        context_key = derive_context_key(uri_norm, exe, locator)
    row["context_key"] = context_key

    # Re-capturing identical content is a new event but the same artifact.
    conn.execute(
        """INSERT OR IGNORE INTO artifact
           (id, kind, mimetype, byte_len, path, locator, captured_at, provenance,
            source_uri, source_uri_norm, source_exe, source_title, context_key,
            source_app, derived_from, derivation)
           VALUES (:id, :kind, :mimetype, :byte_len, :path, :locator, :captured_at, :provenance,
                   :source_uri, :source_uri_norm, :source_exe, :source_title, :context_key,
                   :source_app, :derived_from, :derivation)""",
        row,
    )
    if p.get("capture_id"):
        conn.execute(
            "INSERT OR IGNORE INTO capture_member (capture_id, artifact_id, anchor_id, captured_at)"
            " VALUES (?, ?, NULL, ?)",
            (p["capture_id"], p["id"], p.get("captured_at")),
        )


def _apply_artifact_redacted(conn: sqlite3.Connection, p: dict) -> None:
    # The blob file itself is deleted by the service (once, when the event is
    # first recorded); the projection only tracks the flag so replay stays
    # a pure DB operation.
    conn.execute("UPDATE artifact SET redacted = 1 WHERE id = ?", (p["id"],))


def _apply_artifact_deleted(conn: sqlite3.Connection, p: dict) -> None:
    """Hard delete (ADR 0010): the artifact and everything hanging off it
    leave the projection; nodes and edges stay (they are graph knowledge,
    not capture rows). The blob file is unlinked by the service, never here —
    replay stays a pure DB operation. Proposals keep their decision record
    but drop the dangling anchor reference. Idempotent and total.
    """
    artifact_id = p.get("id")
    if artifact_id is None:
        return
    anchors = "SELECT id FROM anchor WHERE artifact_id = ?"
    conn.execute(f"DELETE FROM support WHERE anchor_id IN ({anchors})", (artifact_id,))
    conn.execute(
        f"UPDATE proposal SET anchor_id = NULL WHERE anchor_id IN ({anchors})", (artifact_id,)
    )
    conn.execute("DELETE FROM anchor WHERE artifact_id = ?", (artifact_id,))
    conn.execute("DELETE FROM capture_member WHERE artifact_id = ?", (artifact_id,))
    conn.execute("UPDATE artifact SET derived_from = NULL WHERE derived_from = ?", (artifact_id,))
    conn.execute("DELETE FROM artifact WHERE id = ?", (artifact_id,))


def _apply_artifact_source_upgraded(conn: sqlite3.Connection, p: dict) -> None:
    """Tier-monotonic: applies only when strictly better than the current tier
    (ADR 0008). Deterministic under replay — the comparison depends only on
    state produced by prior events in the same log order.
    """
    row = conn.execute("SELECT provenance FROM artifact WHERE id = ?", (p["id"],)).fetchone()
    if row is None:
        return
    new_rank = _PROVENANCE_RANK.get(p.get("provenance"), 99)
    if new_rank >= _PROVENANCE_RANK.get(row["provenance"], 99):
        return
    uri = p.get("source_uri")
    uri_norm = p.get("source_uri_norm") or normalize_source_uri(uri)
    conn.execute(
        """UPDATE artifact
           SET provenance = :provenance, source_uri = :source_uri,
               source_uri_norm = :source_uri_norm,
               source_title = COALESCE(:source_title, source_title),
               context_key = COALESCE(:context_key, context_key)
           WHERE id = :id""",
        {
            "id": p["id"],
            "provenance": p.get("provenance"),
            "source_uri": uri,
            "source_uri_norm": uri_norm,
            "source_title": p.get("source_title"),
            "context_key": f"url:{uri_norm}" if uri_norm else None,
        },
    )


def _apply_anchor_added(conn: sqlite3.Connection, p: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO anchor (id, artifact_id, selector_type, selector)"
        " VALUES (?, ?, ?, ?)",
        (p["id"], p["artifact_id"], p["selector_type"], json.dumps(p["selector"], sort_keys=True)),
    )
    if p.get("capture_id"):
        # First anchor of the capture becomes the group's primary anchor.
        conn.execute(
            "UPDATE capture_member SET anchor_id = ?"
            " WHERE capture_id = ? AND artifact_id = ? AND anchor_id IS NULL",
            (p["id"], p["capture_id"], p["artifact_id"]),
        )


def _apply_node_asserted(conn: sqlite3.Connection, p: dict) -> None:
    props = p.get("props") or {}
    label = p["label"]
    if props.get("kind") == "edge_type":
        # Predicates became an ALL_CAPS controlled vocabulary (ADR 0003);
        # normalizing at apply time re-projects pre-vocabulary events too.
        label = normalize_predicate_label(label)
    conn.execute(
        "INSERT OR IGNORE INTO node (id, label, props) VALUES (?, ?, ?)",
        (p["id"], label, json.dumps(props, sort_keys=True)),
    )
    conn.execute(
        "INSERT OR IGNORE INTO node_alias (node_id, surface) VALUES (?, ?)",
        (p["id"], label),
    )


def _apply_edge_asserted(conn: sqlite3.Connection, p: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO edge (id, src, type, dst, props, valid_from, valid_to)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            p["id"],
            p["src"],
            normalize_predicate_label(p["type"]),
            p["dst"],
            json.dumps(p.get("props") or {}, sort_keys=True),
            p.get("valid_from"),
            p.get("valid_to"),
        ),
    )


def _apply_edge_retracted(conn: sqlite3.Connection, p: dict) -> None:
    # The event (with its reason: removed vs edited) stays in the log; only
    # the projection forgets the edge and its evidence links.
    conn.execute("DELETE FROM support WHERE subject_kind = 'edge' AND subject_id = ?", (p["id"],))
    conn.execute("DELETE FROM edge WHERE id = ?", (p["id"],))


def _apply_support_added(conn: sqlite3.Connection, p: dict) -> None:
    # Total applier (ADR 0006): a role or subject_kind this binary's CHECK
    # does not know is a future shape — skip it rather than letting SQLite
    # raise mid-replay.
    if p.get("subject_kind") not in _SUPPORT_SUBJECT_KINDS or p.get("role") not in _SUPPORT_ROLES:
        return
    conn.execute(
        "INSERT OR IGNORE INTO support (subject_kind, subject_id, anchor_id, role)"
        " VALUES (?, ?, ?, ?)",
        (p["subject_kind"], p["subject_id"], p["anchor_id"], p["role"]),
    )


def _apply_support_removed(conn: sqlite3.Connection, p: dict) -> None:
    conn.execute(
        "DELETE FROM support"
        " WHERE subject_kind = ? AND subject_id = ? AND anchor_id = ? AND role = ?",
        (p.get("subject_kind"), p.get("subject_id"), p.get("anchor_id"), p.get("role")),
    )


_APPLIERS: dict[str, Callable[[sqlite3.Connection, dict], None]] = {
    ev.ARTIFACT_ADDED: _apply_artifact_added,
    ev.ARTIFACT_REDACTED: _apply_artifact_redacted,
    ev.ARTIFACT_DELETED: _apply_artifact_deleted,
    ev.ARTIFACT_SOURCE_UPGRADED: _apply_artifact_source_upgraded,
    ev.ANCHOR_ADDED: _apply_anchor_added,
    ev.NODE_ASSERTED: _apply_node_asserted,
    ev.EDGE_ASSERTED: _apply_edge_asserted,
    ev.EDGE_RETRACTED: _apply_edge_retracted,
    ev.SUPPORT_ADDED: _apply_support_added,
    ev.SUPPORT_REMOVED: _apply_support_removed,
}


def apply(conn: sqlite3.Connection, kind: str, payload: dict) -> None:
    applier = _APPLIERS.get(kind)
    if applier is not None:
        applier(conn, payload)


def replay(conn: sqlite3.Connection) -> int:
    """Rebuild the projection from the event log; returns events replayed.

    ``proposal`` is untouched: proposals are not event-sourced yet (M4), so
    clearing that table here would destroy data instead of rebuilding it.
    Every migration that adds a projection table must extend this DELETE list
    in the same change.
    """
    # Children before parents, for foreign keys.
    for table in ("support", "edge", "node_alias", "node", "anchor", "capture_member", "artifact"):
        conn.execute(f"DELETE FROM {table}")
    count = 0
    for row in conn.execute("SELECT kind, payload FROM event ORDER BY seq"):
        apply(conn, row["kind"], json.loads(row["payload"]))
        count += 1
    return count
