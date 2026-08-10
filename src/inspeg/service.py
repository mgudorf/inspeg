"""Capture ingestion and human assertions — the only writers to the store.

Every mutation here goes through ``Store.record`` (event first, projection
second), so the log stays the complete database of record. Writers:
``ingest_clipboard``, ``ingest_web_capture``, ``ingest_code_capture``,
``capture_pointer``, ``apply_label`` / ``remove_label``,
``upgrade_artifact_source``, ``assert_edge`` / ``update_edge`` /
``retract_edge``, ``create_predicate``, ``redact_artifact``,
``delete_artifact``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from inspeg.adapters.cfhtml import CfHtmlError, parse_cf_html
from inspeg.adapters.clipboard import ClipboardSnapshot
from inspeg.model.schemas import (
    CodeSpanSelector,
    Selector,
    TextPositionSelector,
    TextQuoteSelector,
    WholeItemSelector,
)
from inspeg.store import Store
from inspeg.store import events as ev
from inspeg.util import (
    canonical_file_path,
    canonical_json,
    derive_context_key,
    new_id,
    normalize_predicate_label,
    normalize_source_uri,
    split_source_app,
    utcnow_iso,
)

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

# Pointer artifacts (ADR 0005): kinds, target-length sanity bound, and the
# hashing policy — files below the limit get a streamed content hash for rot
# detection; audio/video are never read at all.
POINTER_KINDS = ("url", "file")
POINTER_TARGET_MAX = 4096
POINTER_HASH_LIMIT = 256 * 1024 * 1024
_AV_MIME_PREFIXES = ("audio/", "video/")

# Provenance ranks for upgrade monotonicity (mirror of projection's table).
_PROVENANCE_RANK = {"exact": 1, "sourced": 2, "attributed": 3, "orphan": 4}


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


class UnknownLabelError(KeyError):
    """The label does not exist on this anchor (or at all)."""


class InvalidPointerError(ValueError):
    """The pointer target is malformed or its kind is unknown."""


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
        surface="hotkey",
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
                surface="hotkey",
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
        surface="hotkey",
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
    source_exe: str | None = None,
    source_title: str | None = None,
    context_key: str | None = None,
    surface: str | None = None,
) -> str:
    digest, rel = store.blobs.put(data)
    uri_norm = normalize_source_uri(source_uri)
    if source_exe is None and source_title is None:
        source_exe, source_title = split_source_app(source_app)
    if context_key is None:
        context_key = derive_context_key(uri_norm, source_exe, None)
    store.record(
        ev.ARTIFACT_ADDED,
        {
            "id": digest,
            "kind": "blob",
            "mimetype": mimetype,
            "byte_len": len(data),
            "path": rel,
            "locator": None,
            "captured_at": captured_at,
            "provenance": provenance,
            "source_uri": source_uri,
            "source_uri_norm": uri_norm,
            "source_exe": source_exe,
            "source_title": source_title,
            "context_key": context_key,
            "source_app": source_app,
            "derived_from": None,
            "derivation": None,
            "capture_id": capture_id,
            "surface": surface,
        },
    )
    return digest


def _add_anchor(store: Store, artifact_id: str, selector: Selector, capture_id: str) -> str:
    sel = selector.model_dump(exclude_none=True)
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
    # ours — the DB value names a file we are about to unlink. Pointer
    # artifacts have no path (ADR 0005): the flag is the whole redaction.
    if row["path"] and _BLOB_RELPATH.fullmatch(row["path"]):
        (store.data_dir / row["path"]).unlink(missing_ok=True)


def delete_artifact(store: Store, artifact_id: str, *, actor: str = "human") -> None:
    """Hard-delete one artifact: projection rows, anchors, their labels, and
    the blob file all go (ADR 0010).

    Unlike redaction (which keeps the provenance skeleton), delete leaves
    nothing visible anywhere. The event log keeps only a tombstone —
    ``{"id": …}``, no content — so the log stays append-only and replay
    reproduces the deletion. Nodes and edges are untouched: they are asserted
    knowledge, not capture rows. Idempotent at the API layer via the 404.
    """
    row = store.query_one("SELECT id, path FROM artifact WHERE id = ?", (artifact_id,))
    if row is None:
        raise UnknownArtifactError(artifact_id)
    with store.tx():
        store.record(ev.ARTIFACT_DELETED, {"id": artifact_id}, actor)
    # After the event is durable: unlink the blob, same guard as redaction —
    # only a path shaped like ours, and pointers have no path at all.
    if row["path"] and _BLOB_RELPATH.fullmatch(row["path"]):
        (store.data_dir / row["path"]).unlink(missing_ok=True)


def _normalize_label(label: str) -> str:
    normalized = " ".join(label.split())
    if not normalized:
        raise ValueError("label must not be blank")
    return normalized


# ── Labels (ADR 0006): one-click topical tagging ────────────────────────────


def _record_label(store: Store, anchor_id: str, label: str, *, surface: str, actor: str) -> dict:
    """Topic node + support row. Caller holds the transaction. Idempotent by
    check-then-record so a repeated click appends no duplicate event."""
    node_id, canonical = get_or_create_node(store, label, kind="topic", actor=actor)
    existing = store.query_one(
        "SELECT 1 FROM support WHERE subject_kind = 'node' AND subject_id = ?"
        " AND anchor_id = ? AND role = 'label'",
        (node_id, anchor_id),
    )
    if existing is None:
        store.record(
            ev.SUPPORT_ADDED,
            {
                "subject_kind": "node",
                "subject_id": node_id,
                "anchor_id": anchor_id,
                "role": "label",
                "surface": surface,
            },
            actor,
        )
    return {"id": node_id, "label": canonical, "created": existing is None}


def apply_label(
    store: Store, anchor_id: str, label: str, *, surface: str = "hud", actor: str = "human"
) -> dict:
    """Tag one anchor with a topic label — the one-click primitive."""
    if store.query_one("SELECT id FROM anchor WHERE id = ?", (anchor_id,)) is None:
        raise UnknownAnchorError(anchor_id)
    with store.tx():
        return _record_label(store, anchor_id, label, surface=surface, actor=actor)


def remove_label(store: Store, anchor_id: str, label: str, *, actor: str = "human") -> None:
    """Retract a label; the log keeps the full history (ADR 0006)."""
    label = _normalize_label(label)
    row = store.query_one(
        "SELECT id FROM node WHERE label = ? AND json_extract(props, '$.kind') = 'topic'",
        (label,),
    )
    if row is None:
        raise UnknownLabelError(label)
    existing = store.query_one(
        "SELECT 1 FROM support WHERE subject_kind = 'node' AND subject_id = ?"
        " AND anchor_id = ? AND role = 'label'",
        (row["id"], anchor_id),
    )
    if existing is None:
        raise UnknownLabelError(label)
    with store.tx():
        store.record(
            ev.SUPPORT_REMOVED,
            {
                "subject_kind": "node",
                "subject_id": row["id"],
                "anchor_id": anchor_id,
                "role": "label",
            },
            actor,
        )


def list_labels(store: Store, *, sort: str = "recent", limit: int = 10) -> list[dict]:
    """Topic labels for menus: ``recent`` reads the event log (the log *is*
    the MRU), ``frequent`` counts live support rows. Fully-retracted labels
    drop out of both."""
    limit = max(1, min(limit, 100))
    counts = {
        row["subject_id"]: row["c"]
        for row in store.query(
            "SELECT subject_id, COUNT(*) AS c FROM support"
            " WHERE subject_kind = 'node' AND role = 'label' GROUP BY subject_id"
        )
    }
    if not counts:
        return []
    placeholders = ",".join("?" * len(counts))
    names = {
        row["id"]: row["label"]
        for row in store.query(
            f"SELECT id, label FROM node WHERE id IN ({placeholders})",
            list(counts),
        )
    }
    if sort == "frequent":
        ordered = sorted(counts, key=lambda nid: (-counts[nid], names.get(nid, "")))[:limit]
    else:
        ordered = []
        seen: set[str] = set()
        for row in store.query(
            "SELECT payload FROM event WHERE kind = ? ORDER BY seq DESC LIMIT 1000",
            (ev.SUPPORT_ADDED,),
        ):
            p = json.loads(row["payload"])
            nid = p.get("subject_id")
            if p.get("role") != "label" or nid in seen or nid not in counts:
                continue
            seen.add(nid)
            ordered.append(nid)
            if len(ordered) >= limit:
                break
    return [{"id": nid, "label": names.get(nid, "?"), "count": counts[nid]} for nid in ordered]


# ── Pointer artifacts (ADR 0005) ────────────────────────────────────────────


def _pointer_identity(kind: str, target: str) -> tuple[str, str]:
    """(stable target, pointer id). The id hashes only the stable identity —
    volatile facts ride the payload so re-captures dedupe."""
    if kind not in POINTER_KINDS:
        raise InvalidPointerError(f"unknown pointer kind: {kind!r}")
    target = (target or "").strip()
    if not target or len(target) > POINTER_TARGET_MAX:
        raise InvalidPointerError("pointer target is empty or unreasonably long")
    if kind == "url":
        stable = normalize_source_uri(target)
        if stable is None:
            raise InvalidPointerError(f"not a valid URL: {target!r}")
    else:
        stable = canonical_file_path(target)
    digest = hashlib.sha256(canonical_json({"kind": kind, "target": stable}).encode()).hexdigest()
    return stable, f"pt_{digest}"


def _file_volatile_facts(stable: str, mimetype: str) -> dict:
    """Best-effort stat + streamed hash for file pointers. Audio/video are
    never read (ADR 0005); a vanished file degrades to bare identity."""
    facts: dict = {}
    try:
        st = os.stat(stable)
    except OSError:
        return facts
    facts["byte_len"] = st.st_size
    facts["mtime"] = st.st_mtime_ns
    if st.st_size < POINTER_HASH_LIMIT and not mimetype.startswith(_AV_MIME_PREFIXES):
        try:
            h = hashlib.sha256()
            with open(stable, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            facts["content_sha256"] = h.hexdigest()
        except OSError:
            pass
    return facts


def capture_pointer(
    store: Store,
    *,
    kind: str,
    target: str,
    mimetype: str = "application/octet-stream",
    page_uri: str | None = None,
    source_title: str | None = None,
    source_exe: str | None = None,
    labels: Sequence[str] = (),
    context_key: str | None = None,
    surface: str,
    actor: str = "human",
) -> dict:
    """One metadata-pointer capture: pointer artifact + whole-item anchor +
    labels. Never copies or downloads the target (ADR 0005)."""
    stable, pointer_id = _pointer_identity(kind, target)
    locator: dict = {"kind": kind, "target": stable}
    if kind == "file":
        locator |= _file_volatile_facts(stable, mimetype)
        provenance = "attributed"
        source_uri = page_uri
    else:
        provenance = "sourced"
        source_uri = page_uri or target
    uri_norm = normalize_source_uri(source_uri)
    if context_key is None:
        context_key = derive_context_key(uri_norm, source_exe, locator)
    captured_at = utcnow_iso()
    capture_id = new_id("cap")
    with store.tx():
        store.record(
            ev.ARTIFACT_ADDED,
            {
                "id": pointer_id,
                "kind": "pointer",
                "mimetype": mimetype,
                "byte_len": locator.get("byte_len"),
                "path": None,
                "locator": locator,
                "captured_at": captured_at,
                "provenance": provenance,
                "source_uri": source_uri,
                "source_uri_norm": uri_norm,
                "source_exe": source_exe,
                "source_title": source_title,
                "context_key": context_key,
                "source_app": None,
                "derived_from": None,
                "derivation": None,
                "capture_id": capture_id,
                "surface": surface,
            },
            actor,
        )
        anchor_id = _add_anchor(store, pointer_id, WholeItemSelector(), capture_id)
        applied = [
            _record_label(store, anchor_id, label, surface=surface, actor=actor) for label in labels
        ]
    return {
        "artifact_id": pointer_id,
        "anchor_id": anchor_id,
        "provenance": provenance,
        "captured_at": captured_at,
        "locator": locator,
        "labels": applied,
    }


# ── Web capture (browser extension; ADR 0007) ───────────────────────────────


def ingest_web_capture(
    store: Store,
    *,
    url: str,
    title: str | None = None,
    doc_text: str | None = None,
    selection_exact: str,
    selection_prefix: str = "",
    selection_suffix: str = "",
    selection_start: int | None = None,
    selection_end: int | None = None,
    selection_html: str | None = None,
    labels: Sequence[str] = (),
    surface: str = "browser",
    actor: str = "human",
) -> dict:
    """A browser selection capture.

    HTML pages send ``doc_text`` (the implicit Document artifact — deduped by
    content hash, so revisits reuse it) plus quote/position selectors into it:
    provenance ``exact``. The PDF-viewer path has no document text (Chrome's
    viewer runs no content scripts): the selection itself becomes a text
    artifact plus a pointer to the document URL, provenance ``sourced``.
    """
    for name, value in (
        ("doc_text", doc_text),
        ("selection_exact", selection_exact),
        ("selection_html", selection_html),
    ):
        if value is not None and len(value) > MAX_CAPTURE_BYTES:
            raise CaptureTooLargeError(f"{name} exceeds the capture limit")
    if not selection_exact.strip():
        raise EmptyCaptureError("selection is empty")
    uri_norm = normalize_source_uri(url)
    if uri_norm is None:
        raise InvalidPointerError(f"not a valid URL: {url!r}")
    captured_at = utcnow_iso()
    capture_id = new_id("cap")
    context_key = f"url:{uri_norm}"

    if doc_text is None:
        # PDF / no-content-script path.
        with store.tx():
            artifact_id = _add_artifact(
                store,
                data=selection_exact.encode("utf-8"),
                mimetype="text/plain",
                provenance="sourced",
                source_uri=url,
                source_title=title,
                context_key=context_key,
                capture_id=capture_id,
                captured_at=captured_at,
                surface=surface,
            )
            anchor_id = _add_anchor(
                store,
                artifact_id,
                TextPositionSelector(start=0, end=len(selection_exact)),
                capture_id,
            )
            _stable, doc_pointer_id = _pointer_identity("url", url)
            store.record(
                ev.ARTIFACT_ADDED,
                {
                    "id": doc_pointer_id,
                    "kind": "pointer",
                    "mimetype": "application/pdf",
                    "byte_len": None,
                    "path": None,
                    "locator": {"kind": "url", "target": _stable},
                    "captured_at": captured_at,
                    "provenance": "sourced",
                    "source_uri": url,
                    "source_uri_norm": uri_norm,
                    "source_exe": None,
                    "source_title": title,
                    "context_key": context_key,
                    "source_app": None,
                    "derived_from": None,
                    "derivation": None,
                    "capture_id": capture_id,
                    "surface": surface,
                },
                actor,
            )
            applied = [
                _record_label(store, anchor_id, label, surface=surface, actor=actor)
                for label in labels
            ]
        return {
            "artifact_id": artifact_id,
            "anchor_id": anchor_id,
            "document_artifact_id": doc_pointer_id,
            "position_anchor_id": None,
            "sibling_artifact_ids": [],
            "provenance": "sourced",
            "captured_at": captured_at,
            "excerpt": selection_exact.strip()[:EXCERPT_LIMIT],
            "labels": applied,
        }

    with store.tx():
        document_id = _add_artifact(
            store,
            data=doc_text.encode("utf-8"),
            mimetype="text/plain",
            provenance="exact",
            source_uri=url,
            source_title=title,
            context_key=context_key,
            capture_id=capture_id,
            captured_at=captured_at,
            surface=surface,
        )
        quote_anchor_id = _add_anchor(
            store,
            document_id,
            TextQuoteSelector(
                exact=selection_exact, prefix=selection_prefix, suffix=selection_suffix
            ),
            capture_id,
        )
        position_anchor_id = None
        if selection_start is not None and selection_end is not None:
            position_anchor_id = _add_anchor(
                store,
                document_id,
                TextPositionSelector(start=selection_start, end=selection_end),
                capture_id,
            )
        siblings: list[str] = []
        if selection_html and selection_html.strip():
            siblings.append(
                _add_artifact(
                    store,
                    data=selection_html.encode("utf-8"),
                    mimetype="text/html",
                    provenance="exact",
                    source_uri=url,
                    source_title=title,
                    context_key=context_key,
                    capture_id=capture_id,
                    captured_at=captured_at,
                    surface=surface,
                )
            )
        applied = [
            _record_label(store, quote_anchor_id, label, surface=surface, actor=actor)
            for label in labels
        ]
    return {
        "artifact_id": document_id,
        "anchor_id": quote_anchor_id,
        "document_artifact_id": document_id,
        "position_anchor_id": position_anchor_id,
        "sibling_artifact_ids": siblings,
        "provenance": "exact",
        "captured_at": captured_at,
        "excerpt": selection_exact.strip()[:EXCERPT_LIMIT],
        "labels": applied,
    }


# ── Code capture (VS Code extension) ────────────────────────────────────────


def ingest_code_capture(
    store: Store,
    *,
    text: str,
    path: str,
    start_line: int,
    start_col: int,
    end_line: int,
    end_col: int,
    workspace: str | None = None,
    git_remote: str | None = None,
    git_commit: str | None = None,
    labels: Sequence[str] = (),
    surface: str = "vscode",
    actor: str = "human",
) -> dict:
    """An editor-selection capture: verbatim buffer text + code_span anchor.

    Tier ``exact``: verbatim bytes, a verified file identity, and exact
    offsets — with optional git identity for rot detection.
    """
    if len(text) > MAX_CAPTURE_BYTES:
        raise CaptureTooLargeError("selection exceeds the capture limit")
    if not text.strip():
        raise EmptyCaptureError("selection is empty")
    cpath = canonical_file_path(path)
    source_uri = Path(cpath).as_uri()
    context_key = f"workspace:{canonical_file_path(workspace)}" if workspace else f"file:{cpath}"
    captured_at = utcnow_iso()
    capture_id = new_id("cap")
    with store.tx():
        artifact_id = _add_artifact(
            store,
            data=text.encode("utf-8"),
            mimetype="text/plain",
            provenance="exact",
            source_uri=source_uri,
            source_title=Path(cpath).name,
            context_key=context_key,
            capture_id=capture_id,
            captured_at=captured_at,
            surface=surface,
        )
        anchor_id = _add_anchor(
            store,
            artifact_id,
            CodeSpanSelector(
                path=cpath,
                start_line=start_line,
                start_col=start_col,
                end_line=end_line,
                end_col=end_col,
                git_remote=git_remote,
                git_commit=git_commit,
            ),
            capture_id,
        )
        applied = [
            _record_label(store, anchor_id, label, surface=surface, actor=actor) for label in labels
        ]
    return {
        "artifact_id": artifact_id,
        "anchor_id": anchor_id,
        "provenance": "exact",
        "captured_at": captured_at,
        "excerpt": text.strip()[:EXCERPT_LIMIT],
        "labels": applied,
    }


# ── Provenance tier upgrades (ADR 0008) ─────────────────────────────────────


def upgrade_artifact_source(
    store: Store,
    artifact_id: str,
    *,
    source_uri: str,
    source_title: str | None = None,
    actor: str = "human",
) -> bool:
    """Record a source for an artifact captured without one.

    Tier-monotonic: records (and applies) only when the new tier is strictly
    better than the current one; returns False otherwise. Invariant #3 note:
    ``actor='human'`` is only legitimate when the source rides the same user
    gesture as the capture — a volunteered late upgrade must come in as
    ``proposer:<name>`` and go through the proposal flow.
    """
    row = store.query_one("SELECT provenance FROM artifact WHERE id = ?", (artifact_id,))
    if row is None:
        raise UnknownArtifactError(artifact_id)
    uri_norm = normalize_source_uri(source_uri)
    if uri_norm is None:
        raise InvalidPointerError(f"not a valid URL: {source_uri!r}")
    if _PROVENANCE_RANK["sourced"] >= _PROVENANCE_RANK.get(row["provenance"], 99):
        return False
    with store.tx():
        store.record(
            ev.ARTIFACT_SOURCE_UPGRADED,
            {
                "id": artifact_id,
                "source_uri": source_uri,
                "source_uri_norm": uri_norm,
                "source_title": source_title,
                "provenance": "sourced",
            },
            actor,
        )
    return True


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


def list_edges(
    store: Store, *, limit: int = 100, cursor: int | None = None
) -> tuple[list[dict], int | None]:
    """A page of edges with labels, note, and evidence — the graph-table feed.

    ``cursor`` is the opaque rowid of the last row of the previous page;
    returns (rows, next_cursor). Unbounded listing died with the HUD plan —
    every read path paginates.
    """
    limit = max(1, min(limit, 200))
    where = ""
    params: list = []
    if cursor is not None:
        where = "WHERE e.rowid < ?"
        params.append(cursor)
    rows = store.query(
        f"""SELECT e.rowid AS rid, e.id, e.type, e.props,
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
           {where}
           ORDER BY e.rowid DESC LIMIT ?""",
        [*params, limit],
    )
    next_cursor = rows[-1]["rid"] if len(rows) == limit else None
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
    ], next_cursor
