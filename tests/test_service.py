import pytest

from helpers import build_cf_html
from inspeg import service
from inspeg.adapters.clipboard import ClipboardSnapshot


def test_html_capture_with_source_url_is_tier_2(store):
    snap = ClipboardSnapshot(
        cf_html=build_cf_html("<p>Kùzu is <b>embedded</b></p>", source_url="https://kuzudb.com/"),
        text="Kùzu is embedded",
        source_app="chrome.exe | Kùzu",
    )
    capture = service.ingest_clipboard(store, snap)
    assert capture.provenance == "sourced"
    assert capture.source_url == "https://kuzudb.com/"
    assert capture.excerpt == "Kùzu is embedded"

    artifact = store.query_one("SELECT * FROM artifact WHERE id = ?", (capture.artifact_id,))
    assert artifact["mimetype"] == "text/html"
    assert artifact["provenance"] == "sourced"
    assert artifact["source_uri"] == "https://kuzudb.com/"

    # The plain-text sibling from the same copy is kept too.
    assert len(capture.sibling_artifact_ids) == 1
    sibling = store.query_one(
        "SELECT * FROM artifact WHERE id = ?", (capture.sibling_artifact_ids[0],)
    )
    assert sibling["mimetype"] == "text/plain"

    # The anchor selects exactly the copied fragment inside the stored blob.
    anchor = store.query_one("SELECT * FROM anchor WHERE id = ?", (capture.anchor_id,))
    assert anchor["selector_type"] == "text_position"
    import json

    selector = json.loads(anchor["selector"])
    html = store.blobs.get(capture.artifact_id).decode("utf-8")
    assert html[selector["start"] : selector["end"]] == "<p>Kùzu is <b>embedded</b></p>"


def test_html_capture_without_source_url_keeps_only_text(store):
    """App-local HTML (editors, IDEs) is styling junk around the text; without
    a SourceURL there is no provenance value in keeping it, so only the plain
    text is stored (see docs/provenance.md)."""
    styled = '<div style="color:#a5d6ff;font-family:Consolas">KL divergence</div>'
    snap = ClipboardSnapshot(
        cf_html=build_cf_html(styled), text="KL divergence", source_app="Code.exe | notes"
    )
    capture = service.ingest_clipboard(store, snap)
    assert capture.provenance == "attributed"
    assert capture.source_url is None
    artifact = store.query_one("SELECT * FROM artifact WHERE id = ?", (capture.artifact_id,))
    assert artifact["mimetype"] == "text/plain"
    assert store.blobs.get(capture.artifact_id).decode() == "KL divergence"
    # No HTML artifact at all — one text/plain artifact is the whole capture.
    assert store.query_one("SELECT COUNT(*) AS c FROM artifact")["c"] == 1


def test_html_without_source_url_or_text_sibling_derives_text(store):
    snap = ClipboardSnapshot(
        cf_html=build_cf_html('<div style="x">only <b>html</b></div>'),
        source_app="word.exe | Doc1",
    )
    capture = service.ingest_clipboard(store, snap)
    assert capture.provenance == "attributed"
    assert store.blobs.get(capture.artifact_id).decode() == "only html"


def test_text_only_capture_with_app_is_tier_3(store):
    snap = ClipboardSnapshot(text="plain words", source_app="notepad.exe | notes.txt")
    capture = service.ingest_clipboard(store, snap)
    assert capture.provenance == "attributed"
    artifact = store.query_one("SELECT * FROM artifact WHERE id = ?", (capture.artifact_id,))
    assert artifact["mimetype"] == "text/plain"
    assert artifact["source_app"] == "notepad.exe | notes.txt"


def test_text_only_capture_without_app_is_tier_4(store):
    capture = service.ingest_clipboard(store, ClipboardSnapshot(text="mystery"))
    assert capture.provenance == "orphan"


def test_empty_clipboard_raises(store):
    with pytest.raises(service.EmptyCaptureError):
        service.ingest_clipboard(store, ClipboardSnapshot(text="   "))


def test_recapturing_same_span_is_idempotent(store):
    snap = ClipboardSnapshot(cf_html=build_cf_html("<p>same</p>", source_url="https://s.test/"))
    first = service.ingest_clipboard(store, snap)
    second = service.ingest_clipboard(store, snap)
    assert first.anchor_id == second.anchor_id
    assert first.artifact_id == second.artifact_id
    assert store.query_one("SELECT COUNT(*) AS c FROM anchor")["c"] == 1
    assert store.query_one("SELECT COUNT(*) AS c FROM artifact")["c"] == 1
    # ...but the log remembers both capture events.
    assert store.query_one("SELECT COUNT(*) AS c FROM event WHERE kind = 'anchor_added'")["c"] == 2


def test_assert_edge_creates_nodes_type_node_and_support(store):
    capture = service.ingest_clipboard(
        store,
        ClipboardSnapshot(
            cf_html=build_cf_html("<p>MSFT is a tech company</p>", source_url="https://n.test/")
        ),
    )
    result = service.assert_edge(
        store,
        anchor_id=capture.anchor_id,
        src_label="  Microsoft   Corp. ",
        edge_type="instance of",
        dst_label="Tech Company",
        note="from the quiz",
        create_predicate=True,
    )
    assert result["src"]["label"] == "Microsoft Corp."  # whitespace normalized
    assert result["type"]["label"] == "INSTANCE_OF"  # predicates are ALL_CAPS

    edge = store.query_one("SELECT * FROM edge WHERE id = ?", (result["id"],))
    assert edge["type"] == "INSTANCE_OF"

    # Types are nodes, not strings (§3.4).
    type_node = store.query_one(
        "SELECT * FROM node WHERE label = 'INSTANCE_OF' "
        "AND json_extract(props, '$.kind') = 'edge_type'"
    )
    assert type_node is not None
    assert type_node["id"] == result["type"]["id"]

    support = store.query_one(
        "SELECT * FROM support WHERE subject_kind = 'edge' AND subject_id = ?", (result["id"],)
    )
    assert support["anchor_id"] == capture.anchor_id
    assert support["role"] == "evidence"

    import json

    props = json.loads(edge["props"])
    assert props["context"] == "from the quiz"


def test_assert_edge_reuses_existing_nodes(store):
    capture = service.ingest_clipboard(store, ClipboardSnapshot(text="x", source_app="a.exe"))
    first = service.assert_edge(
        store,
        anchor_id=capture.anchor_id,
        src_label="A",
        edge_type="REL",
        dst_label="B",
        create_predicate=True,
    )
    second = service.assert_edge(
        store, anchor_id=capture.anchor_id, src_label="A", edge_type="REL", dst_label="C"
    )
    assert first["src"]["id"] == second["src"]["id"]
    assert first["type"]["id"] == second["type"]["id"]


def test_assert_edge_unknown_anchor(store):
    with pytest.raises(service.UnknownAnchorError):
        service.assert_edge(
            store, anchor_id="anc_nope", src_label="A", edge_type="rel", dst_label="B"
        )


def test_replay_after_real_session_is_stable(store):
    capture = service.ingest_clipboard(
        store, ClipboardSnapshot(cf_html=build_cf_html("<p>r</p>", source_url="https://r.test/"))
    )
    service.assert_edge(
        store,
        anchor_id=capture.anchor_id,
        src_label="A",
        edge_type="REL",
        dst_label="B",
        create_predicate=True,
    )
    counts_before = {
        table: store.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        for table in ("artifact", "anchor", "node", "edge", "support")
    }
    store.replay()
    counts_after = {
        table: store.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        for table in ("artifact", "anchor", "node", "edge", "support")
    }
    assert counts_after == counts_before
