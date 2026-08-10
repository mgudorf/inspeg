"""Curation features: hard delete (ADR 0010), context ignore list, and the
graph viewer's read model (search + hyperlinked node pages)."""

import pytest
from fastapi.testclient import TestClient

from conftest import BASE_URL
from inspeg import queries, service
from inspeg.api.app import create_app
from inspeg.context import DEFAULT_IGNORED_EXES, ContextHub
from inspeg.fts import FtsIndex
from test_multi_surface import dump_projection, web_capture

CAPTURE = {"X-Inspeg-Capture": "1"}


# ── delete: store level ─────────────────────────────────────────────────────


def test_delete_removes_everything_but_the_node(store):
    result = web_capture(store, labels=["Topic A"])
    artifact_id = result["artifact_id"]
    blob_path = (
        store.data_dir
        / store.query_one("SELECT path FROM artifact WHERE id = ?", (artifact_id,))["path"]
    )
    assert blob_path.exists()

    service.delete_artifact(store, artifact_id)

    assert store.query_one("SELECT 1 FROM artifact WHERE id = ?", (artifact_id,)) is None
    assert store.query("SELECT 1 FROM anchor WHERE artifact_id = ?", (artifact_id,)) == []
    assert store.query("SELECT 1 FROM support") == []
    assert store.query("SELECT 1 FROM capture_member WHERE artifact_id = ?", (artifact_id,)) == []
    assert not blob_path.exists()
    # The topic node is asserted knowledge, not a capture row: it survives.
    assert store.query_one("SELECT 1 FROM node WHERE label = 'Topic A'") is not None


def test_delete_unknown_artifact_raises(store):
    with pytest.raises(service.UnknownArtifactError):
        service.delete_artifact(store, "no_such")


def test_delete_pointer_artifact_has_no_blob_to_unlink(store):
    result = service.capture_pointer(
        store, kind="url", target="https://example.com/img.png", surface="browser"
    )
    service.delete_artifact(store, result["artifact_id"])
    assert store.query_one("SELECT 1 FROM artifact WHERE id = ?", (result["artifact_id"],)) is None


def test_delete_keeps_edges_but_drops_their_evidence(store):
    result = web_capture(store)
    service.assert_edge(
        store,
        src_label="A",
        edge_type="RELATES_TO",
        dst_label="B",
        anchor_id=result["anchor_id"],
        create_predicate=True,
    )
    service.delete_artifact(store, result["artifact_id"])
    edge = store.query_one("SELECT id FROM edge")
    assert edge is not None
    assert (
        store.query(
            "SELECT 1 FROM support WHERE subject_kind = 'edge' AND subject_id = ?", (edge["id"],)
        )
        == []
    )


def test_delete_replays_deterministically(store):
    web_capture(store, labels=["Keep Me"])
    doomed = web_capture(store, url="https://other.example/x", doc_text="Doomed text here")
    service.delete_artifact(store, doomed["artifact_id"])
    before = dump_projection(store)
    store.replay()
    assert dump_projection(store) == before


def test_deleted_artifact_vanishes_from_every_read_path(store):
    result = web_capture(store)
    service.delete_artifact(store, result["artifact_id"])
    resolved = queries.resolve_context(store, url="https://example.com/a?a=1&b=2")
    assert resolved["items"] == []
    assert queries.unannotated_queue(store) == []
    assert all(
        group["count"] == 0 or group["key"] != "url:https://example.com/a?a=1&b=2"
        for group in queries.tree(store)["groups"]
    )


def test_delete_reaches_fts_synchronously(store, tmp_path):
    fts = FtsIndex(store, tmp_path / "cache.db", start_worker=False)
    store.on_commit.append(fts.on_commit)
    try:
        result = web_capture(store, doc_text="searchable zanzibar text")
        fts.process_pending()
        assert fts.search("zanzibar")
        service.delete_artifact(store, result["artifact_id"])
        assert fts.search("zanzibar") == []
    finally:
        fts.close()


def test_fts_reconcile_purges_deleted_artifacts_on_reopen(store, tmp_path):
    fts = FtsIndex(store, tmp_path / "cache.db", start_worker=False)
    store.on_commit.append(fts.on_commit)
    result = web_capture(store, doc_text="reconcile quux text")
    fts.process_pending()
    assert fts.search("quux")
    fts.close()
    store.on_commit.remove(fts.on_commit)
    # Delete while no index is attached — the crash-window scenario.
    service.delete_artifact(store, result["artifact_id"])
    reopened = FtsIndex(store, tmp_path / "cache.db", start_worker=False)
    try:
        assert reopened.search("quux") == []
    finally:
        reopened.close()


# ── delete: API ─────────────────────────────────────────────────────────────


def test_delete_endpoint(client, store):
    result = web_capture(store)
    response = client.delete(f"/api/artifacts/{result['artifact_id']}", headers=CAPTURE)
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert (
        client.delete(f"/api/artifacts/{result['artifact_id']}", headers=CAPTURE).status_code == 404
    )


def test_delete_endpoint_requires_capture_header(client, store):
    result = web_capture(store)
    assert client.delete(f"/api/artifacts/{result['artifact_id']}").status_code == 403
    assert store.query_one("SELECT 1 FROM artifact WHERE id = ?", (result["artifact_id"],))


# ── context ignore list ─────────────────────────────────────────────────────


def test_default_ignored_exes_clear_the_window_slot():
    hub = ContextHub()
    hub.set_window(exe="notepad.exe", title="notes.txt")
    assert hub.snapshot()["window"]["exe"] == "notepad.exe"
    for noisy in ("python.exe", "Chrome.EXE", "code.exe"):
        hub.set_window(exe=noisy, title="whatever")
        assert hub.snapshot()["window"] is None, noisy
        hub.set_window(exe="notepad.exe", title="notes.txt")


def test_ignored_exe_clears_rather_than_keeping_stale_window():
    hub = ContextHub()
    hub.set_window(exe="acrobat.exe", title="paper.pdf")
    hub.set_window(exe="chrome.exe", title="Some Tab")
    # A stale acrobat.exe here would claim the user is still in the PDF.
    assert hub.snapshot()["window"] is None


def test_custom_ignore_list_extends_defaults_in_main_wiring():
    hub = ContextHub(ignored_exes=DEFAULT_IGNORED_EXES | {"myapp.exe"})
    hub.set_window(exe="MyApp.exe", title="x")
    assert hub.snapshot()["window"] is None
    hub.set_window(exe="other.exe", title="y")
    assert hub.snapshot()["window"]["exe"] == "other.exe"


def test_health_reports_ignored_exes(store):
    hub = ContextHub()
    with TestClient(create_app(store, context_hub=hub), base_url=BASE_URL) as client:
        health = client.get("/api/health").json()
        assert health["ignored_exes"] == sorted(DEFAULT_IGNORED_EXES)


# ── graph viewer read model ─────────────────────────────────────────────────


@pytest.fixture
def graphed(store):
    """A small graph: one labeled capture, one edge, two topics."""
    capture = web_capture(store, labels=["Machine Learning", "Bias"])
    edge = service.assert_edge(
        store,
        src_label="Neural Networks",
        edge_type="EXHIBITS",
        dst_label="Bias",
        anchor_id=capture["anchor_id"],
        create_predicate=True,
    )
    return {"capture": capture, "edge": edge}


def test_graph_search_spans_every_node_kind(client, graphed):
    nodes = client.get("/api/graph/search", params={"q": ""}).json()["nodes"]
    kinds = {n["label"]: n["kind"] for n in nodes}
    assert kinds["Machine Learning"] == "topic"
    assert kinds["EXHIBITS"] == "edge_type"
    assert kinds["Neural Networks"] is None


def test_graph_search_matches_substring(client, graphed):
    nodes = client.get("/api/graph/search", params={"q": "learn"}).json()["nodes"]
    assert [n["label"] for n in nodes] == ["Machine Learning"]


def test_graph_node_detail_links_both_directions(client, store, graphed):
    edge = graphed["edge"]
    src = client.get(f"/api/graph/nodes/{edge['src']['id']}").json()
    assert src["out_edges"][0]["type"] == "EXHIBITS"
    assert src["out_edges"][0]["other"]["label"] == "Bias"
    dst = client.get(f"/api/graph/nodes/{edge['dst']['id']}").json()
    assert dst["in_edges"][0]["other"]["label"] == "Neural Networks"


def test_graph_node_detail_co_labels_and_item_count(client, store, graphed):
    nodes = {
        n["label"]: n for n in client.get("/api/graph/search", params={"q": ""}).json()["nodes"]
    }
    detail = client.get(f"/api/graph/nodes/{nodes['Machine Learning']['id']}").json()
    assert detail["label_count"] == 1
    assert [co["label"] for co in detail["co_labels"]] == ["Bias"]


def test_graph_node_detail_404s(client):
    assert client.get("/api/graph/nodes/n_missing").status_code == 404
