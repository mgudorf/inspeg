"""Capture ingestion and human assertions — the only writers to the store.

Every mutation here goes through ``Store.record`` (event first, projection
second), so the log stays the complete database of record.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from inspeg.adapters.cfhtml import CfHtmlError, parse_cf_html
from inspeg.adapters.clipboard import ClipboardSnapshot
from inspeg.model.schemas import TextPositionSelector
from inspeg.store import Store
from inspeg.store import events as ev
from inspeg.util import canonical_json, new_id, utcnow_iso

EXCERPT_LIMIT = 500
# Excerpts come from at most this much raw HTML: parsing an entire pasted
# document to show 500 characters is how a big copy hangs the daemon.
EXCERPT_HTML_SLICE = 64 * 1024
# One clipboard snapshot may not exceed this many bytes/chars per format.
# Clipboard text beyond this is almost certainly a mistake, and every byte is
# decoded, parsed, hashed, and stored — the cap bounds memory and disk.
MAX_CAPTURE_BYTES = 16 * 1024 * 1024

_BLOB_RELPATH = re.compile(r"blobs/[0-9a-f]{2}/[0-9a-f]{64}")


class EmptyCaptureError(ValueError):
    """Nothing usable was on the clipboard."""


class CaptureTooLargeError(ValueError):
    """The clipboard payload exceeds MAX_CAPTURE_BYTES."""


class UnknownAnchorError(KeyError):
    """The referenced anchor does not exist."""


class UnknownArtifactError(KeyError):
    """The referenced artifact does not exist."""


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

    provenance = "sourced" if cf.source_url else "attributed"
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


def assert_edge(
    store: Store,
    *,
    anchor_id: str,
    src_label: str,
    edge_type: str,
    dst_label: str,
    note: str | None = None,
    actor: str = "human",
) -> dict:
    """Assert one typed edge supported by one anchor as evidence."""
    if store.query_one("SELECT id FROM anchor WHERE id = ?", (anchor_id,)) is None:
        raise UnknownAnchorError(anchor_id)
    with store.tx():
        src_id, src = get_or_create_node(store, src_label, actor=actor)
        type_id, type_label = get_or_create_node(store, edge_type, kind="edge_type", actor=actor)
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
        "anchor_id": anchor_id,
    }
