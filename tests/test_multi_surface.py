"""Phase-1 store foundation: labels (ADR 0006), pointer artifacts (ADR 0005),
context identity (ADR 0008), web/code captures, and replay compatibility."""

import json
import os

import pytest

from inspeg import service
from inspeg.adapters.clipboard import ClipboardSnapshot
from inspeg.store import events as ev
from inspeg.util import (
    canonical_file_path,
    derive_context_key,
    normalize_source_uri,
    split_source_app,
)

PROJECTION_TABLES = (
    "artifact",
    "anchor",
    "node",
    "node_alias",
    "edge",
    "support",
    "capture_member",
)


def dump_projection(store):
    return {
        table: [tuple(row) for row in store.query(f"SELECT * FROM {table} ORDER BY 1, 2")]
        for table in PROJECTION_TABLES
    }


def web_capture(store, **overrides):
    kwargs = dict(
        url="https://Example.com:443/a/?b=2&a=1#frag",
        title="A Page",
        doc_text="Alpha beta gamma delta epsilon",
        selection_exact="beta gamma",
        selection_prefix="Alpha ",
        selection_suffix=" delta",
        selection_start=6,
        selection_end=16,
        labels=(),
    )
    kwargs.update(overrides)
    return service.ingest_web_capture(store, **kwargs)


# ── util helpers (ADR 0008) ─────────────────────────────────────────────────


def test_normalize_source_uri_canonicalizes():
    assert (
        normalize_source_uri("https://Example.com:443/a/?b=2&a=1#frag")
        == "https://example.com/a?a=1&b=2"
    )
    assert normalize_source_uri("http://x.com:8080/p/") == "http://x.com:8080/p"
    assert normalize_source_uri("https://x.com/") == "https://x.com"


def test_normalize_source_uri_is_total():
    assert normalize_source_uri(None) is None
    assert normalize_source_uri("") is None
    assert normalize_source_uri("not a url") is None
    assert normalize_source_uri("http://[broken") is None


def test_split_source_app():
    assert split_source_app("chrome.exe | Some Page") == ("chrome.exe", "Some Page")
    assert split_source_app("notepad.EXE") == ("notepad.EXE", None)
    assert split_source_app("Just a title") == (None, "Just a title")
    assert split_source_app(None) == (None, None)
    assert split_source_app("   ") == (None, None)


def test_derive_context_key_priority():
    assert derive_context_key("https://x.com/a", "chrome.exe", None) == "url:https://x.com/a"
    assert derive_context_key(None, "chrome.exe", {"kind": "file", "target": "/p/f"}) == "file:/p/f"
    assert derive_context_key(None, "chrome.exe", None) == "app:chrome.exe"
    assert derive_context_key(None, None, None) is None


def test_canonical_file_path_is_absolute_and_idempotent(tmp_path):
    p = canonical_file_path(str(tmp_path / "sub" / ".." / "x.txt"))
    assert os.path.isabs(p)
    assert ".." not in p
    assert canonical_file_path(p) == p


# ── web captures ────────────────────────────────────────────────────────────


def test_web_capture_document_and_both_anchors(store):
    result = web_capture(store)
    art = store.query_one("SELECT * FROM artifact WHERE id = ?", (result["artifact_id"],))
    assert art["kind"] == "blob"
    assert art["provenance"] == "exact"
    assert art["source_uri_norm"] == "https://example.com/a?a=1&b=2"
    assert art["context_key"] == "url:https://example.com/a?a=1&b=2"
    assert art["source_title"] == "A Page"
    quote = store.query_one("SELECT * FROM anchor WHERE id = ?", (result["anchor_id"],))
    assert quote["selector_type"] == "text_quote"
    assert json.loads(quote["selector"])["exact"] == "beta gamma"
    position = store.query_one("SELECT * FROM anchor WHERE id = ?", (result["position_anchor_id"],))
    assert position["selector_type"] == "text_position"


def test_web_capture_dedupes_document_across_visits(store):
    first = web_capture(store)
    second = web_capture(store, selection_exact="delta epsilon", selection_prefix="gamma ")
    assert first["artifact_id"] == second["artifact_id"]
    count = store.query_one(
        "SELECT COUNT(*) AS c FROM artifact WHERE id = ?", (first["artifact_id"],)
    )
    assert count["c"] == 1
    assert first["anchor_id"] != second["anchor_id"]


def test_web_capture_pdf_variant_stores_text_plus_pointer(store):
    result = web_capture(store, doc_text=None, url="https://arxiv.org/pdf/1234.5678v1")
    assert result["provenance"] == "sourced"
    text_art = store.query_one("SELECT * FROM artifact WHERE id = ?", (result["artifact_id"],))
    assert text_art["kind"] == "blob"
    pointer = store.query_one(
        "SELECT * FROM artifact WHERE id = ?", (result["document_artifact_id"],)
    )
    assert pointer["kind"] == "pointer"
    assert pointer["mimetype"] == "application/pdf"
    assert pointer["path"] is None
    assert pointer["id"].startswith("pt_")


def test_web_capture_rejects_empty_and_oversized(store):
    with pytest.raises(service.EmptyCaptureError):
        web_capture(store, selection_exact="   ")
    with pytest.raises(service.CaptureTooLargeError):
        web_capture(store, doc_text="x" * (service.MAX_CAPTURE_BYTES + 1))
    with pytest.raises(service.InvalidPointerError):
        web_capture(store, url="not a url")


# ── code captures ───────────────────────────────────────────────────────────


def test_code_capture_exact_tier_with_span(store, tmp_path):
    path = str(tmp_path / "mod.py")
    result = service.ingest_code_capture(
        store,
        text="def f():\n    return 1\n",
        path=path,
        start_line=10,
        start_col=0,
        end_line=11,
        end_col=12,
        workspace=str(tmp_path),
        git_commit="abc123",
        labels=["Code Patterns"],
    )
    art = store.query_one("SELECT * FROM artifact WHERE id = ?", (result["artifact_id"],))
    assert art["provenance"] == "exact"
    assert art["context_key"] == f"workspace:{canonical_file_path(str(tmp_path))}"
    assert art["source_uri"].startswith("file://")
    anchor = store.query_one("SELECT * FROM anchor WHERE id = ?", (result["anchor_id"],))
    sel = json.loads(anchor["selector"])
    assert sel["type"] == "code_span"
    assert sel["start_line"] == 10
    assert sel["git_commit"] == "abc123"
    assert "git_remote" not in sel  # None fields stay out of the deterministic id
    assert result["labels"][0]["label"] == "Code Patterns"


# ── labels (ADR 0006) ───────────────────────────────────────────────────────


def test_apply_label_creates_topic_node_and_support(store):
    result = web_capture(store)
    applied = service.apply_label(store, result["anchor_id"], "AI Knowledge", surface="browser")
    node = store.query_one("SELECT * FROM node WHERE id = ?", (applied["id"],))
    assert node["label"] == "AI Knowledge"
    assert json.loads(node["props"])["kind"] == "topic"
    row = store.query_one(
        "SELECT * FROM support WHERE subject_kind = 'node' AND subject_id = ?", (applied["id"],)
    )
    assert row["anchor_id"] == result["anchor_id"]
    assert row["role"] == "label"


def test_apply_label_reuses_topic_node_across_captures(store):
    a = web_capture(store)
    b = web_capture(store, url="https://other.example/x", doc_text="Something else entirely")
    first = service.apply_label(store, a["anchor_id"], "AI Knowledge")
    second = service.apply_label(store, b["anchor_id"], "AI Knowledge")
    assert first["id"] == second["id"]
    count = store.query_one(
        "SELECT COUNT(*) AS c FROM support WHERE subject_id = ? AND role = 'label'",
        (first["id"],),
    )
    assert count["c"] == 2


def test_apply_label_is_idempotent(store):
    result = web_capture(store)
    service.apply_label(store, result["anchor_id"], "Topic X")
    events_before = store.query_one("SELECT COUNT(*) AS c FROM event")["c"]
    again = service.apply_label(store, result["anchor_id"], "Topic X")
    assert again["created"] is False
    assert store.query_one("SELECT COUNT(*) AS c FROM event")["c"] == events_before


def test_apply_label_unknown_anchor_raises(store):
    with pytest.raises(service.UnknownAnchorError):
        service.apply_label(store, "anc_missing", "X")


def test_remove_label_retracts_projection_keeps_log(store):
    result = web_capture(store)
    applied = service.apply_label(store, result["anchor_id"], "Ephemeral")
    service.remove_label(store, result["anchor_id"], "Ephemeral")
    assert (
        store.query_one(
            "SELECT 1 FROM support WHERE subject_id = ? AND role = 'label'", (applied["id"],)
        )
        is None
    )
    removed_events = store.query("SELECT payload FROM event WHERE kind = ?", (ev.SUPPORT_REMOVED,))
    assert len(removed_events) == 1
    store.replay()  # the log must not resurrect the label
    assert (
        store.query_one(
            "SELECT 1 FROM support WHERE subject_id = ? AND role = 'label'", (applied["id"],)
        )
        is None
    )


def test_remove_unknown_label_raises(store):
    result = web_capture(store)
    with pytest.raises(service.UnknownLabelError):
        service.remove_label(store, result["anchor_id"], "Never Applied")


def test_list_labels_recent_and_frequent(store):
    a = web_capture(store)
    b = web_capture(store, url="https://b.example/x", doc_text="Doc b")
    c = web_capture(store, url="https://c.example/x", doc_text="Doc c")
    service.apply_label(store, a["anchor_id"], "Common")
    service.apply_label(store, b["anchor_id"], "Common")
    service.apply_label(store, c["anchor_id"], "Rare")
    service.apply_label(store, a["anchor_id"], "Latest")

    frequent = service.list_labels(store, sort="frequent", limit=10)
    assert frequent[0]["label"] == "Common"
    assert frequent[0]["count"] == 2

    recent = service.list_labels(store, sort="recent", limit=10)
    assert [r["label"] for r in recent] == ["Latest", "Rare", "Common"]


# ── pointer artifacts (ADR 0005) ────────────────────────────────────────────


def test_capture_pointer_url_dedupes_on_normalized_identity(store):
    first = service.capture_pointer(
        store, kind="url", target="https://Example.com/img.png?b=2&a=1#x", surface="browser"
    )
    second = service.capture_pointer(
        store, kind="url", target="https://example.com/img.png?a=1&b=2", surface="browser"
    )
    assert first["artifact_id"] == second["artifact_id"]
    assert first["artifact_id"].startswith("pt_")
    count = store.query_one(
        "SELECT COUNT(*) AS c FROM artifact WHERE id = ?", (first["artifact_id"],)
    )
    assert count["c"] == 1


def test_capture_pointer_file_hashes_and_survives_touch(store, tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    first = service.capture_pointer(
        store, kind="file", target=str(target), mimetype="text/plain", surface="vscode"
    )
    assert first["locator"]["content_sha256"]
    assert first["locator"]["byte_len"] == 5
    os.utime(target, (1, 1))  # metadata change must not mint a new artifact
    second = service.capture_pointer(
        store, kind="file", target=str(target), mimetype="text/plain", surface="vscode"
    )
    assert second["artifact_id"] == first["artifact_id"]


def test_capture_pointer_av_is_never_read(store, tmp_path):
    target = tmp_path / "talk.mp4"
    target.write_bytes(b"fake video bytes")
    result = service.capture_pointer(
        store, kind="file", target=str(target), mimetype="video/mp4", surface="vscode"
    )
    assert "content_sha256" not in result["locator"]
    assert result["locator"]["byte_len"] == 16  # stat is fine; reading is not


def test_capture_pointer_rejects_bad_input(store):
    with pytest.raises(service.InvalidPointerError):
        service.capture_pointer(store, kind="carrier-pigeon", target="x", surface="hud")
    with pytest.raises(service.InvalidPointerError):
        service.capture_pointer(store, kind="url", target="", surface="hud")
    with pytest.raises(service.InvalidPointerError):
        service.capture_pointer(store, kind="url", target="notaurl", surface="hud")


def test_pointer_rows_are_invisible_to_the_sweep(store):
    blob = web_capture(store)
    service.capture_pointer(store, kind="url", target="https://x.com/a.png", surface="browser")
    store._sweep_stale_files()
    blob_path = store.query_one("SELECT path FROM artifact WHERE id = ?", (blob["artifact_id"],))[
        "path"
    ]
    assert (store.data_dir / blob_path).exists()


def test_redacting_a_pointer_flags_without_deleting(store):
    result = service.capture_pointer(
        store, kind="url", target="https://x.com/secret.png", surface="browser"
    )
    service.redact_artifact(store, result["artifact_id"])
    row = store.query_one("SELECT redacted FROM artifact WHERE id = ?", (result["artifact_id"],))
    assert row["redacted"] == 1


# ── provenance upgrades (ADR 0008) ──────────────────────────────────────────


def test_upgrade_artifact_source_is_tier_monotonic(store):
    capture = service.ingest_clipboard(store, ClipboardSnapshot(text="pasted somewhere"))
    assert capture.provenance == "orphan"
    assert service.upgrade_artifact_source(
        store, capture.artifact_id, source_uri="https://found.example/later", source_title="Found"
    )
    row = store.query_one("SELECT * FROM artifact WHERE id = ?", (capture.artifact_id,))
    assert row["provenance"] == "sourced"
    assert row["source_uri_norm"] == "https://found.example/later"
    assert row["context_key"] == "url:https://found.example/later"
    # Already sourced: a second upgrade to the same tier is refused.
    assert not service.upgrade_artifact_source(
        store, capture.artifact_id, source_uri="https://elsewhere.example"
    )
    store.replay()
    row = store.query_one("SELECT provenance FROM artifact WHERE id = ?", (capture.artifact_id,))
    assert row["provenance"] == "sourced"


def test_upgrade_never_downgrades_exact(store):
    result = web_capture(store)  # exact
    assert not service.upgrade_artifact_source(
        store, result["artifact_id"], source_uri="https://worse.example"
    )
    row = store.query_one("SELECT provenance FROM artifact WHERE id = ?", (result["artifact_id"],))
    assert row["provenance"] == "exact"


def test_upgrade_unknown_artifact_raises(store):
    with pytest.raises(service.UnknownArtifactError):
        service.upgrade_artifact_source(store, "missing", source_uri="https://x.com")


# ── replay compatibility ────────────────────────────────────────────────────


def test_legacy_artifact_payload_derives_context_columns(store):
    """Pre-0004 payloads (no kind/locator/context fields) must project with
    best-effort derived columns — the ADR 0003 two-layer pattern."""
    digest, rel = store.blobs.put(b"legacy bytes")
    with store.tx():
        store.record(
            ev.ARTIFACT_ADDED,
            {
                "id": digest,
                "mimetype": "text/plain",
                "byte_len": 12,
                "path": rel,
                "captured_at": "2026-01-01T00:00:00.000+00:00",
                "provenance": "sourced",
                "source_uri": "https://Old.example/Page?z=1&a=2#f",
                "source_app": "chrome.exe | Old Page",
                "derived_from": None,
                "derivation": None,
                "capture_id": "cap_legacy",
            },
        )
    row = store.query_one("SELECT * FROM artifact WHERE id = ?", (digest,))
    assert row["kind"] == "blob"
    assert row["source_exe"] == "chrome.exe"
    assert row["source_title"] == "Old Page"
    assert row["source_uri_norm"] == "https://old.example/Page?a=2&z=1"
    assert row["context_key"] == "url:https://old.example/Page?a=2&z=1"
    member = store.query_one("SELECT * FROM capture_member WHERE capture_id = 'cap_legacy'")
    assert member["artifact_id"] == digest


def test_unknown_support_role_is_skipped_not_fatal(store):
    result = web_capture(store)
    with store.tx():
        store.record(
            ev.SUPPORT_ADDED,
            {
                "subject_kind": "node",
                "subject_id": "n_x",
                "anchor_id": result["anchor_id"],
                "role": "hologram",  # future role this binary has never heard of
            },
        )
    assert store.query_one("SELECT 1 FROM support WHERE role = 'hologram'") is None
    store.replay()  # must not raise
    assert store.query_one("SELECT 1 FROM support WHERE role = 'hologram'") is None


def test_capture_member_groups_artifacts_and_primary_anchor(store):
    result = web_capture(store)
    member = store.query_one(
        "SELECT * FROM capture_member WHERE artifact_id = ?", (result["artifact_id"],)
    )
    assert member is not None
    assert member["anchor_id"] == result["anchor_id"]  # first anchor of the capture


def test_replay_rebuilds_identical_projection_with_new_kinds(store, tmp_path):
    target = tmp_path / "f.bin"
    target.write_bytes(b"x")
    a = web_capture(store, labels=["Alpha", "Beta"])
    web_capture(store, doc_text=None, url="https://arxiv.org/pdf/1.2")
    service.capture_pointer(store, kind="file", target=str(target), surface="vscode")
    service.ingest_code_capture(
        store, text="x = 1", path=str(target), start_line=1, start_col=0, end_line=1, end_col=5
    )
    service.remove_label(store, a["anchor_id"], "Beta")
    capture = service.ingest_clipboard(store, ClipboardSnapshot(text="hello"))
    service.upgrade_artifact_source(store, capture.artifact_id, source_uri="https://s.example")
    before = dump_projection(store)
    store.replay()
    assert dump_projection(store) == before


# ── ContextHub (ADR 0004): pure state + TTLs ────────────────────────────────


def test_context_hub_ttls_and_publish():
    from inspeg.context import ContextHub

    now = [0.0]
    published = []
    hub = ContextHub(on_change=published.append, clock=lambda: now[0])
    hub.set_tab("https://x.com/a", "A")
    hub.set_workspace("C:/w", "C:/w/main.py")
    # acrobat.exe: a real context app, not on the default ignore list
    hub.set_window(exe="acrobat.exe", title="A page")
    snap = hub.snapshot()
    assert snap["tab"]["url"] == "https://x.com/a"
    assert snap["workspace"]["root"] == "C:/w"
    assert snap["window"]["exe"] == "acrobat.exe"
    assert len(published) == 3

    now[0] = 20.0  # tab TTL (15s) elapsed; workspace (60s) still fresh
    snap = hub.snapshot()
    assert snap["tab"] is None
    assert snap["workspace"] is not None
    now[0] = 90.0
    assert hub.snapshot()["workspace"] is None
    # window identity never expires — it is replaced, not aged out
    assert hub.snapshot()["window"]["exe"] == "acrobat.exe"


def test_context_hub_fullscreen_dedupes_and_validates():
    from inspeg.context import ContextHub

    published = []
    hub = ContextHub(on_change=published.append)
    hub.set_fullscreen("d3d")
    hub.set_fullscreen("d3d")  # no change, no publish
    hub.set_fullscreen("weird-new-state")  # unknown -> none
    assert [p["fullscreen"] for p in published] == ["d3d", "none"]
