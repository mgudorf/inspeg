"""Capture ingestion and human assertions — the only writers to the store.

Every mutation here goes through ``Store.record`` (event first, projection
second), so the log stays the complete database of record.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from inspeg.adapters.cfhtml import CfHtmlError, parse_cf_html
from inspeg.adapters.clipboard import ClipboardSnapshot
from inspeg.model.schemas import TextPositionSelector
from inspeg.store import Store
from inspeg.store import events as ev
from inspeg.util import canonical_json, new_id, normalize_predicate_label, utcnow_iso

EXCERPT_LIMIT = 500
# Excerpts come from at most this much raw HTML: parsing an entire pasted
# document to show 500 characters is how a big copy hangs the daemon.
EXCERPT_HTML_SLICE = 64 * 1024
# One clipboard snapshot may not exceed this many bytes/chars per format.
# Clipboard text beyond this is almost certainly a mistake, and every byte is
# decoded, parsed, hashed, and stored — the cap bounds memory and disk.
MAX_CAPTURE_BYTES = 16 * 1024 * 1024

_BLOB_RELPATH = re.compile(r"blobs/[0-9a-f]{2}/[0-9a-f]{64}")

# Predicates are a controlled vocabulary of ALL_CAPS identifiers (ADR 0003).
PREDICATE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class EmptyCaptureError(ValueError):
    """Nothing usable was on the clipboard."""


class CaptureTooLargeError(ValueError):
    """The clipboard payload exceeds MAX_CAPTURE_BYTES."""


class UnknownAnchorError(KeyError):
    """The referenced anchor does not exist."""


class UnknownArtifactError(KeyError):
    """The referenced artifact does not exist."""


class UnknownEdgeError(KeyError):
    """The referenced edge does not exist (or was already retracted)."""


class UnknownPredicateError(KeyError):
    """The predicate is not in the vocabulary and creation was not requested."""


@dataclass(frozen=True)
class Capture:
    anchor_id: str
    artifact_id: str
    provenance: str
    captured_at: str
    excerpt: str
    source_url: str | None = None
    source_app: str | None = None
    sibling_artifact_ids: tuple[str, ...] = field(default_factory=tuple)


def html_to_text(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def safe_url(url: str | None) -> str | None:
    """Return ``url`` only if it is safe to render as a hyperlink.

    CF_HTML ``SourceURL`` is attacker-controllable (any app can write the
    clipboard), so schemes like ``javascript:`` or ``data:`` must never reach
    an ``href``. The raw value stays in ``artifact.source_uri`` as provenance.
    """
    if not url:
        return None
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return None
    return url if scheme in ("http", "https") else None


def _anchor_id(artifact_id: str, selector: dict) -> str:
    """Deterministic anchor id: re-capturing the same span is idempotent."""
    digest = hashlib.sha256(f"{artifact_id}:{canonical_json(selector)}".encode()).hexdigest()
    return f"anc_{digest[:24]}"


def ingest_clipboard(store: Store, snap: ClipboardSnapshot) -> Capture:
    """One clipboard snapshot -> artifact(s) + one anchor over the copied span."""
    if not snap.cf_html and not (snap.text or "").strip():
        raise EmptyCaptureError("clipboard has neither HTML nor text")
    if len(snap.cf_html or b"") > MAX_CAPTURE_BYTES or len(snap.text or "") > MAX_CAPTURE_BYTES:
        raise CaptureTooLargeError(
            f"clipboard payload exceeds the {MAX_CAPTURE_BYTES // (1024 * 1024)} MiB capture limit"
        )
    captured_at = utcnow_iso()
    capture_id = new_id("cap")  # groups sibling artifacts in the event log
    with store.tx():
        if snap.cf_html:
            return _ingest_html(store, snap, capture_id, captured_at)
        return _ingest_text(store, snap, capture_id, captured_at)


def _ingest_html(
    store: Store, snap: ClipboardSnapshot, capture_id: str, captured_at: str
) -> Capture:
    try:
        cf = parse_cf_html(snap.cf_html or b"")
    except CfHtmlError as exc:
        if (snap.text or "").strip():
            return _ingest_text(store, snap, capture_id, captured_at)
        raise EmptyCaptureError(f"malformed CF_HTML and no text fallback: {exc}") from exc

    if not cf.source_url:
        # App-local HTML (editors, IDEs, office apps) is styling markup around
        # the text — <div style="..."> wrappers with no provenance value, since
        # without a SourceURL the tier is 'attributed' either way. Keep only
        # the text: the CF_UNICODETEXT sibling if present, else the fragment
        # stripped of tags. See docs/provenance.md.
        if not (snap.text or "").strip():
            derived = html_to_text(cf.fragment)
            if not derived.strip():
                raise EmptyCaptureError("clipboard HTML contained no text")
            snap = replace(snap, text=derived)
        return _ingest_text(store, snap, capture_id, captured_at)

    provenance = "sourced"
    artifact_id = _add_artifact(
        store,
        data=cf.html.encode("utf-8"),
        mimetype="text/html",
        provenance=provenance,
        source_uri=cf.source_url,
        source_app=snap.source_app,
        capture_id=capture_id,
        captured_at=captured_at,
    )
    anchor_id = _add_anchor(
        store,
        artifact_id,
        TextPositionSelector(start=cf.fragment_start, end=cf.fragment_end),
        capture_id,
    )

    # One copy often yields HTML and plain text at once; keep both as siblings.
    siblings: list[str] = []
    if (snap.text or "").strip():
        siblings.append(
            _add_artifact(
                store,
                data=(snap.text or "").encode("utf-8"),
                mimetype="text/plain",
                provenance=provenance,
                source_uri=cf.source_url,
                source_app=snap.source_app,
                capture_id=capture_id,
                captured_at=captured_at,
            )
        )

    return Capture(
        anchor_id=anchor_id,
        artifact_id=artifact_id,
        sibling_artifact_ids=tuple(siblings),
        provenance=provenance,
        source_url=cf.source_url,
        source_app=snap.source_app,
        captured_at=captured_at,
        excerpt=html_to_text(cf.fragment[:EXCERPT_HTML_SLICE])[:EXCERPT_LIMIT],
    )


def _ingest_text(
    store: Store, snap: ClipboardSnapshot, capture_id: str, captured_at: str
) -> Capture:
    text = snap.text or ""
    provenance = "attributed" if snap.source_app else "orphan"
    artifact_id = _add_artifact(
        store,
        data=text.encode("utf-8"),
        mimetype="text/plain",
        provenance=provenance,
        source_uri=None,
        source_app=snap.source_app,
        capture_id=capture_id,
        captured_at=captured_at,
    )
    anchor_id = _add_anchor(
        store, artifact_id, TextPositionSelector(start=0, end=len(text)), capture_id
    )
    return Capture(
        anchor_id=anchor_id,
        artifact_id=artifact_id,
        provenance=provenance,
        source_app=snap.source_app,
        captured_at=captured_at,
        excerpt=text.strip()[:EXCERPT_LIMIT],
    )


def _add_artifact(
    store: Store,
    *,
    data: bytes,
    mimetype: str,
    provenance: str,
    capture_id: str,
    captured_at: str,
    source_uri: str | None = None,
    source_app: str | None = None,
) -> str:
    digest, rel = store.blobs.put(data)
    store.record(
        ev.ARTIFACT_ADDED,
        {
            "id": digest,
            "mimetype": mimetype,
            "byte_len": len(data),
            "path": rel,
            "captured_at": captured_at,
            "provenance": provenance,
            "source_uri": source_uri,
            "source_app": source_app,
            "derived_from": None,
            "derivation": None,
            "capture_id": capture_id,
        },
    )
    return digest


def _add_anchor(
    store: Store, artifact_id: str, selector: TextPositionSelector, capture_id: str
) -> str:
    sel = selector.model_dump()
    anchor_id = _anchor_id(artifact_id, sel)
    store.record(
        ev.ANCHOR_ADDED,
        {
            "id": anchor_id,
            "artifact_id": artifact_id,
            "selector_type": sel["type"],
            "selector": sel,
            "capture_id": capture_id,
        },
    )
    return anchor_id


def redact_artifact(store: Store, artifact_id: str, *, actor: str = "human") -> None:
    """Destroy an artifact's content while keeping its provenance skeleton.

    The one sanctioned exception to blob immutability, for captures that never
    should have happened (a copied password, private correspondence). Records
    an ``artifact_redacted`` event — the log stays append-only and replay
    reproduces the flag — then deletes the blob file. Idempotent.
    """
    row = store.query_one("SELECT id, path, redacted FROM artifact WHERE id = ?", (artifact_id,))
    if row is None:
        raise UnknownArtifactError(artifact_id)
    if not row["redacted"]:
        with store.tx():
            store.record(ev.ARTIFACT_REDACTED, {"id": artifact_id}, actor)
    # After the event is durable: delete the file, but only a path shaped like
    # ours — the DB value names a file we are about to unlink.
    if _BLOB_RELPATH.fullmatch(row["path"]):
        (store.data_dir / row["path"]).unlink(missing_ok=True)


def _normalize_label(label: str) -> str:
    normalized = " ".join(label.split())
    if not normalized:
        raise ValueError("label must not be blank")
    return normalized


def get_or_create_node(
    store: Store, label: str, *, kind: str | None = None, actor: str = "human"
) -> tuple[str, str]:
    """Return (node_id, canonical label), creating the node if needed.

    ``kind`` distinguishes edge-type nodes (§3.4: types are nodes, not strings)
    from plain entities; it lives in ``props.kind``, never in a label column.
    """
    label = _normalize_label(label)
    row = store.query_one(
        "SELECT id FROM node WHERE label = ? AND json_extract(props, '$.kind') IS ?",
        (label, kind),
    )
    if row:
        return row["id"], label
    node_id = new_id("n")
    props = {"kind": kind} if kind else {}
    store.record(ev.NODE_ASSERTED, {"id": node_id, "label": label, "props": props}, actor)
    return node_id, label


def normalize_predicate(label: str) -> str:
    """Normalize and validate a predicate label against the vocabulary rules."""
    normalized = normalize_predicate_label(label)
    if not PREDICATE_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"invalid predicate {label!r}: predicates are ALL_CAPS identifiers "
            "(letters, digits, underscores; must start with a letter)"
        )
    return normalized


def list_predicates(store: Store) -> list[dict]:
    rows = store.query(
        "SELECT id, label FROM node WHERE json_extract(props, '$.kind') = 'edge_type'"
        " ORDER BY label"
    )
    return [{"id": row["id"], "label": row["label"]} for row in rows]


def create_predicate(store: Store, label: str, *, actor: str = "human") -> dict:
    """Add a predicate to the vocabulary. Deliberately a separate action from
    asserting an edge (§10.2: a new edge type costs one extra click)."""
    normalized = normalize_predicate(label)
    with store.tx():
        node_id, normalized = get_or_create_node(store, normalized, kind="edge_type", actor=actor)
    return {"id": node_id, "label": normalized}


def _resolve_predicate(
    store: Store, edge_type: str, *, create: bool, actor: str
) -> tuple[str, str]:
    label = normalize_predicate(edge_type)
    row = store.query_one(
        "SELECT id FROM node WHERE label = ? AND json_extract(props, '$.kind') = 'edge_type'",
        (label,),
    )
    if row is not None:
        return row["id"], label
    if not create:
        raise UnknownPredicateError(label)
    return get_or_create_node(store, label, kind="edge_type", actor=actor)


def _record_edge(
    store: Store,
    *,
    src_label: str,
    edge_type: str,
    dst_label: str,
    anchor_ids: Sequence[str],
    note: str | None,
    create_predicate: bool,
    actor: str,
) -> dict:
    """Node resolution + edge/support events. Caller holds the transaction."""
    src_id, src = get_or_create_node(store, src_label, actor=actor)
    type_id, type_label = _resolve_predicate(store, edge_type, create=create_predicate, actor=actor)
    dst_id, dst = get_or_create_node(store, dst_label, actor=actor)
    edge_id = new_id("e")
    props = {"context": note.strip()} if note and note.strip() else {}
    store.record(
        ev.EDGE_ASSERTED,
        {
            "id": edge_id,
            "src": src_id,
            "type": type_label,  # denormalized into the projection for query speed
            "type_node_id": type_id,
            "dst": dst_id,
            "props": props,
            "valid_from": None,
            "valid_to": None,
        },
        actor,
    )
    for anchor_id in anchor_ids:
        store.record(
            ev.SUPPORT_ADDED,
            {
                "subject_kind": "edge",
                "subject_id": edge_id,
                "anchor_id": anchor_id,
                "role": "evidence",
            },
            actor,
        )
    return {
        "id": edge_id,
        "src": {"id": src_id, "label": src},
        "type": {"id": type_id, "label": type_label},
        "dst": {"id": dst_id, "label": dst},
        "anchor_id": anchor_ids[0] if anchor_ids else None,
        "note": props.get("context"),
    }


def assert_edge(
    store: Store,
    *,
    src_label: str,
    edge_type: str,
    dst_label: str,
    anchor_id: str | None = None,
    note: str | None = None,
    create_predicate: bool = False,
    actor: str = "human",
) -> dict:
    """Assert one typed edge, optionally supported by one anchor as evidence.

    ``anchor_id=None`` records a manual, unevidenced assertion — legitimate,
    but visibly weaker: the graph view shows its evidence count as zero.
    """
    if anchor_id is not None and (
        store.query_one("SELECT id FROM anchor WHERE id = ?", (anchor_id,)) is None
    ):
        raise UnknownAnchorError(anchor_id)
    with store.tx():
        return _record_edge(
            store,
            src_label=src_label,
            edge_type=edge_type,
            dst_label=dst_label,
            anchor_ids=[anchor_id] if anchor_id else [],
            note=note,
            create_predicate=create_predicate,
            actor=actor,
        )


def retract_edge(
    store: Store, edge_id: str, *, reason: str = "removed", actor: str = "human"
) -> None:
    """Remove an edge from the projection; the log keeps the full history."""
    if store.query_one("SELECT id FROM edge WHERE id = ?", (edge_id,)) is None:
        raise UnknownEdgeError(edge_id)
    with store.tx():
        store.record(ev.EDGE_RETRACTED, {"id": edge_id, "reason": reason}, actor)


def update_edge(
    store: Store,
    edge_id: str,
    *,
    src_label: str,
    edge_type: str,
    dst_label: str,
    note: str | None = None,
    create_predicate: bool = False,
    actor: str = "human",
) -> dict:
    """Edit = retract + re-assert in one transaction; evidence carries over.

    The corrected edge gets a new id and the log records both steps (the
    retraction carries ``reason: edited``) — corrections are the valuable
    part of the corpus, so they are never rewritten in place.
    """
    if store.query_one("SELECT id FROM edge WHERE id = ?", (edge_id,)) is None:
        raise UnknownEdgeError(edge_id)
    anchor_ids = [
        row["anchor_id"]
        for row in store.query(
            "SELECT anchor_id FROM support"
            " WHERE subject_kind = 'edge' AND subject_id = ? AND role = 'evidence'",
            (edge_id,),
        )
    ]
    with store.tx():
        store.record(ev.EDGE_RETRACTED, {"id": edge_id, "reason": "edited"}, actor)
        return _record_edge(
            store,
            src_label=src_label,
            edge_type=edge_type,
            dst_label=dst_label,
            anchor_ids=anchor_ids,
            note=note,
            create_predicate=create_predicate,
            actor=actor,
        )


def list_edges(store: Store) -> list[dict]:
    """Every edge with its labels, note, and evidence — the graph-table feed."""
    rows = store.query(
        """SELECT e.id, e.type, e.props,
                  s.id AS src_id, s.label AS src_label,
                  d.id AS dst_id, d.label AS dst_label,
                  (SELECT COUNT(*) FROM support sp
                    WHERE sp.subject_kind = 'edge' AND sp.subject_id = e.id
                      AND sp.role = 'evidence') AS evidence,
                  (SELECT sp.anchor_id FROM support sp
                    WHERE sp.subject_kind = 'edge' AND sp.subject_id = e.id
                      AND sp.role = 'evidence' LIMIT 1) AS anchor_id
           FROM edge e
           JOIN node s ON s.id = e.src
           JOIN node d ON d.id = e.dst
           ORDER BY e.rowid DESC"""
    )
    return [
        {
            "id": row["id"],
            "src": {"id": row["src_id"], "label": row["src_label"]},
            "type": row["type"],
            "dst": {"id": row["dst_id"], "label": row["dst_label"]},
            "note": (json.loads(row["props"]) or {}).get("context"),
            "evidence": row["evidence"],
            "anchor_id": row["anchor_id"],
        }
        for row in rows
    ]
