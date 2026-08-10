"""Read-side queries for the HUD and the extensions.

Read-only by construction: everything here goes through ``Store.read_query``
(the WAL snapshot connection), so a busy HUD can never stall a capture.
Redaction is honored on every excerpt path, and pointer artifacts are
branched before any blob access (ADR 0005 — a ``pt_`` id through the blob
store is a crash, not a 410).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from inspeg import service
from inspeg.store import Store
from inspeg.util import canonical_file_path, normalize_source_uri

TREE_GROUP_LIMIT = 200
ITEM_LIMIT = 200


def _clamp(limit: int, ceiling: int) -> int:
    return max(1, min(limit, ceiling))


def artifact_excerpt(store: Store, row) -> str | None:
    """Bounded excerpt for a text blob; None for redacted/pointer/binary."""
    if row["redacted"] or row["kind"] != "blob" or not row["mimetype"].startswith("text/"):
        return None
    try:
        data = store.blobs.get(row["id"])
    except (FileNotFoundError, ValueError):
        return None
    text = data.decode("utf-8", "replace")
    if row["mimetype"] == "text/html":
        text = service.html_to_text(text[: service.EXCERPT_HTML_SLICE])
    return text.strip()[: service.EXCERPT_LIMIT]


def _artifact_dict(row) -> dict:
    out = {
        "id": row["id"],
        "kind": row["kind"],
        "mimetype": row["mimetype"],
        "provenance": row["provenance"],
        "captured_at": row["captured_at"],
        "source_uri": row["source_uri"],
        # Scheme-validated: the only value a UI may place in an href (V4).
        "source_link": service.safe_url(row["source_uri"]),
        "source_title": row["source_title"],
        "source_exe": row["source_exe"],
        "context_key": row["context_key"],
        "redacted": bool(row["redacted"]),
    }
    if row["kind"] == "pointer" and row["locator"]:
        out["locator"] = json.loads(row["locator"])
    return out


def _items_for_artifacts(store: Store, rows) -> list[dict]:
    items = []
    for row in rows:
        anchors = [
            {
                "id": a["id"],
                "selector_type": a["selector_type"],
                "selector": json.loads(a["selector"]),
            }
            for a in store.read_query(
                "SELECT id, selector_type, selector FROM anchor WHERE artifact_id = ?"
                " ORDER BY rowid",
                (row["id"],),
            )
        ]
        labels = [
            {"id": lr["id"], "label": lr["label"]}
            for lr in store.read_query(
                """SELECT DISTINCT n.id, n.label
                   FROM support s
                   JOIN anchor a ON a.id = s.anchor_id
                   JOIN node n ON n.id = s.subject_id
                   WHERE a.artifact_id = ? AND s.subject_kind = 'node' AND s.role = 'label'
                   ORDER BY n.label""",
                (row["id"],),
            )
        ]
        items.append(
            {
                "artifact": _artifact_dict(row),
                "anchors": anchors,
                "labels": labels,
                "excerpt": artifact_excerpt(store, row),
            }
        )
    return items


_ARTIFACT_COLS = (
    "rowid, id, kind, mimetype, provenance, captured_at, source_uri, source_uri_norm,"
    " source_title, source_exe, context_key, locator, redacted"
)


def resolve_context(
    store: Store,
    *,
    url: str | None = None,
    path: str | None = None,
    exe: str | None = None,
    key: str | None = None,
    limit: int = 50,
    cursor: int | None = None,
) -> dict:
    """Everything captured from HERE — one indexed query (ADR 0008).

    ``key`` is an exact ``context_key`` match (how the HUD expands a tree
    group, including ``workspace:`` keys which have no URL/path form).
    """
    given = [v for v in (url, path, exe, key) if v]
    if len(given) != 1:
        raise ValueError("pass exactly one of url=, path=, exe=, key=")
    limit = _clamp(limit, ITEM_LIMIT)
    if url is not None:
        norm = normalize_source_uri(url)
        if norm is None:
            return {"context_key": None, "items": [], "next_cursor": None}
        context_key = f"url:{norm}"
        where, params = "source_uri_norm = ?", [norm]
    elif path is not None:
        cpath = canonical_file_path(path)
        norm = normalize_source_uri(Path(cpath).as_uri())
        context_key = f"file:{cpath}"
        where, params = "(source_uri_norm = ? OR context_key = ?)", [norm, context_key]
    elif key is not None:
        context_key = key
        where, params = "context_key = ?", [key]
    else:
        context_key = f"app:{exe}"
        where, params = "source_exe = ?", [exe]
    if cursor is not None:
        where += " AND rowid < ?"
        params.append(cursor)
    rows = store.read_query(
        f"SELECT {_ARTIFACT_COLS} FROM artifact WHERE {where} ORDER BY rowid DESC LIMIT ?",
        [*params, limit],
    )
    next_cursor = rows[-1]["rowid"] if len(rows) == limit else None
    return {
        "context_key": context_key,
        "items": _items_for_artifacts(store, rows),
        "next_cursor": next_cursor,
    }


def tree(store: Store, *, group_by: str = "context", limit: int = 50, offset: int = 0) -> dict:
    """The HUD's top-level tree: groups only; items load lazily via
    ``resolve_context`` / ``label_items``."""
    limit = _clamp(limit, TREE_GROUP_LIMIT)
    offset = max(0, offset)
    if group_by == "label":
        rows = store.read_query(
            """SELECT n.id, n.label, COUNT(*) AS c
               FROM support s JOIN node n ON n.id = s.subject_id
               WHERE s.subject_kind = 'node' AND s.role = 'label'
               GROUP BY n.id ORDER BY c DESC, n.label LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        groups = [
            {"key": r["id"], "kind": "label", "display": r["label"], "count": r["c"]} for r in rows
        ]
    elif group_by == "context":
        rows = store.read_query(
            """SELECT context_key, COUNT(*) AS c, MAX(captured_at) AS latest
               FROM artifact GROUP BY context_key
               ORDER BY latest DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        groups = []
        for r in rows:
            key = r["context_key"]
            kind, _, display = (key or "").partition(":")
            groups.append(
                {
                    "key": key,
                    "kind": kind or "none",
                    "display": display or "uncategorized",
                    "count": r["c"],
                    "latest": r["latest"],
                }
            )
    else:
        raise ValueError("group_by must be 'context' or 'label'")
    next_offset = offset + limit if len(groups) == limit else None
    return {"groups": groups, "next_offset": next_offset}


def label_items(store: Store, node_id: str, *, limit: int = 50, cursor: int | None = None) -> dict:
    """Every item carrying one label — the 'show me everything about X' query
    (unique-index prefix on support)."""
    limit = _clamp(limit, ITEM_LIMIT)
    where = ""
    params: list = [node_id]
    if cursor is not None:
        where = " AND art.rowid < ?"
        params.append(cursor)
    rows = store.read_query(
        f"""SELECT DISTINCT {", ".join("art." + c.strip() for c in _ARTIFACT_COLS.split(","))}
            FROM support s
            JOIN anchor a ON a.id = s.anchor_id
            JOIN artifact art ON art.id = a.artifact_id
            WHERE s.subject_kind = 'node' AND s.subject_id = ? AND s.role = 'label'{where}
            ORDER BY art.rowid DESC LIMIT ?""",
        [*params, limit],
    )
    next_cursor = rows[-1]["rowid"] if len(rows) == limit else None
    return {"items": _items_for_artifacts(store, rows), "next_cursor": next_cursor}


def similar_items(store: Store, anchor_id: str, *, limit: int = 25) -> list[dict]:
    """Anchors sharing nodes (labels or evidenced subjects) with this one,
    ranked by how many they share."""
    limit = _clamp(limit, ITEM_LIMIT)
    rows = store.read_query(
        """SELECT s2.anchor_id AS anchor_id, COUNT(DISTINCT s2.subject_id) AS shared
           FROM support s1
           JOIN support s2 ON s2.subject_kind = s1.subject_kind
                          AND s2.subject_id = s1.subject_id
           WHERE s1.anchor_id = ? AND s2.anchor_id != ?
           GROUP BY s2.anchor_id ORDER BY shared DESC, s2.anchor_id LIMIT ?""",
        (anchor_id, anchor_id, limit),
    )
    out = []
    for r in rows:
        art = store.read_query_one(
            f"""SELECT {_ARTIFACT_COLS} FROM artifact
                WHERE id = (SELECT artifact_id FROM anchor WHERE id = ?)""",
            (r["anchor_id"],),
        )
        if art is None:
            continue
        out.append(
            {
                "anchor_id": r["anchor_id"],
                "shared": r["shared"],
                "artifact": _artifact_dict(art),
                "excerpt": artifact_excerpt(store, art),
            }
        )
    return out


def unannotated_queue(store: Store, *, limit: int = 20) -> list[dict]:
    """Recent captures with no labels yet — the HUD's annotate-later queue
    (fixes M0's "only the last capture is reachable")."""
    limit = _clamp(limit, ITEM_LIMIT)
    rows = store.read_query(
        f"""SELECT {_ARTIFACT_COLS} FROM artifact art
            WHERE art.redacted = 0 AND NOT EXISTS (
                SELECT 1 FROM support s
                JOIN anchor a ON a.id = s.anchor_id
                WHERE a.artifact_id = art.id
                  AND s.subject_kind = 'node' AND s.role = 'label'
            )
            AND art.derived_from IS NULL
            ORDER BY art.rowid DESC LIMIT ?""",
        (limit,),
    )
    return _items_for_artifacts(store, rows)


# ── Graph viewer (hyperlinked node navigation) ──────────────────────────────


def search_graph_nodes(store: Store, q: str, *, limit: int = 25) -> list[dict]:
    """Substring search over every node — topics, entities, predicates —
    ranked by how connected they are. The graph search bar's feed."""
    limit = _clamp(limit, 100)
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = store.read_query(
        r"""SELECT n.id, n.label, json_extract(n.props, '$.kind') AS kind,
                   (SELECT COUNT(*) FROM support s
                     WHERE s.subject_kind = 'node' AND s.subject_id = n.id
                       AND s.role = 'label') AS label_count,
                   (SELECT COUNT(*) FROM edge e
                     WHERE e.src = n.id OR e.dst = n.id) AS edge_count
            FROM node n
            WHERE n.label LIKE ? ESCAPE '\'
            ORDER BY label_count + edge_count DESC, n.label LIMIT ?""",
        (f"%{escaped}%", limit),
    )
    return [
        {
            "id": r["id"],
            "label": r["label"],
            "kind": r["kind"],
            "label_count": r["label_count"],
            "edge_count": r["edge_count"],
        }
        for r in rows
    ]


def node_detail(store: Store, node_id: str) -> dict | None:
    """One node with everything a hyperlinked graph page needs: its edges
    (both directions, with the other endpoint), the labels that co-occur on
    the same items, and how many items carry it. Items themselves load via
    ``label_items`` — this stays one cheap page."""
    node = store.read_query_one("SELECT id, label, props FROM node WHERE id = ?", (node_id,))
    if node is None:
        return None
    props = json.loads(node["props"] or "{}")

    def _edges(direction: str) -> list[dict]:
        here, there = ("src", "dst") if direction == "out" else ("dst", "src")
        return [
            {"id": r["id"], "type": r["type"], "other": {"id": r["oid"], "label": r["olabel"]}}
            for r in store.read_query(
                f"""SELECT e.id, e.type, n.id AS oid, n.label AS olabel
                    FROM edge e JOIN node n ON n.id = e.{there}
                    WHERE e.{here} = ? ORDER BY e.type, n.label LIMIT 200""",
                (node_id,),
            )
        ]

    co_labels = [
        {"id": r["id"], "label": r["label"], "shared": r["shared"]}
        for r in store.read_query(
            """SELECT n2.id, n2.label, COUNT(DISTINCT s2.anchor_id) AS shared
               FROM support s1
               JOIN support s2 ON s2.anchor_id = s1.anchor_id
                              AND s2.subject_id != s1.subject_id
               JOIN node n2 ON n2.id = s2.subject_id
               WHERE s1.subject_kind = 'node' AND s1.subject_id = ? AND s1.role = 'label'
                 AND s2.subject_kind = 'node' AND s2.role = 'label'
               GROUP BY n2.id ORDER BY shared DESC, n2.label LIMIT 20""",
            (node_id,),
        )
    ]
    count_row = store.read_query_one(
        "SELECT COUNT(DISTINCT anchor_id) AS c FROM support"
        " WHERE subject_kind = 'node' AND subject_id = ? AND role = 'label'",
        (node_id,),
    )
    return {
        "id": node["id"],
        "label": node["label"],
        "kind": props.get("kind"),
        "out_edges": _edges("out"),
        "in_edges": _edges("in"),
        "co_labels": co_labels,
        "label_count": count_row["c"] if count_row else 0,
    }


def url_digests(store: Store) -> list[str]:
    """SHA-256 digests of every normalized captured URL, for the extension's
    local highlight cache — so it never has to ask per visited page (the
    browsing-history privacy trap)."""
    rows = store.read_query(
        "SELECT DISTINCT source_uri_norm FROM artifact"
        " WHERE source_uri_norm IS NOT NULL AND redacted = 0"
    )
    return sorted(hashlib.sha256(r["source_uri_norm"].encode("utf-8")).hexdigest() for r in rows)
