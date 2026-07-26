import pytest
from fastapi.testclient import TestClient

from helpers import build_cf_html
from inspeg import service
from inspeg.adapters.clipboard import ClipboardSnapshot
from inspeg.api.app import create_app


@pytest.fixture
def client(store):
    return TestClient(create_app(store))


@pytest.fixture
def capture(store):
    snap = ClipboardSnapshot(
        cf_html=build_cf_html(
            "<p><b>SQLite</b> is public domain</p>", source_url="https://sqlite.org/"
        ),
        text="SQLite is public domain",
        source_app="chrome.exe | SQLite",
    )
    return service.ingest_clipboard(store, snap)


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True


def test_latest_anchor_404_when_empty(client):
    assert client.get("/api/anchors/latest").status_code == 404


def test_anchor_detail(client, capture):
    body = client.get(f"/api/anchors/{capture.anchor_id}").json()
    assert body["anchor"]["id"] == capture.anchor_id
    assert body["artifact"]["provenance"] == "sourced"
    assert body["artifact"]["source_uri"] == "https://sqlite.org/"
    assert body["excerpt"] == "SQLite is public domain"


def test_latest_anchor_returns_most_recent(client, capture):
    body = client.get("/api/anchors/latest").json()
    assert body["anchor"]["id"] == capture.anchor_id


def test_assert_edge_flow(client, capture, store):
    response = client.post(
        "/api/edges",
        json={
            "anchor_id": capture.anchor_id,
            "src_label": "SQLite",
            "edge_type": "has_license",
            "dst_label": "Public Domain",
            "note": None,
        },
    )
    assert response.status_code == 200, response.text
    edge = response.json()
    assert edge["src"]["label"] == "SQLite"
    assert edge["type"]["label"] == "has_license"

    stats = client.get("/api/stats").json()
    assert stats["edge"] == 1
    assert stats["node"] == 3  # SQLite, Public Domain, has_license (types are nodes)

    support = store.query_one("SELECT * FROM support WHERE subject_id = ?", (edge["id"],))
    assert support["anchor_id"] == capture.anchor_id


def test_assert_edge_unknown_anchor_404(client):
    response = client.post(
        "/api/edges",
        json={"anchor_id": "anc_missing", "src_label": "A", "edge_type": "r", "dst_label": "B"},
    )
    assert response.status_code == 404


def test_assert_edge_blank_label_rejected(client, capture):
    response = client.post(
        "/api/edges",
        json={"anchor_id": capture.anchor_id, "src_label": "", "edge_type": "r", "dst_label": "B"},
    )
    assert response.status_code == 422


def test_node_search_separates_entities_from_edge_types(client, capture):
    client.post(
        "/api/edges",
        json={
            "anchor_id": capture.anchor_id,
            "src_label": "SQLite",
            "edge_type": "has_license",
            "dst_label": "Public Domain",
        },
    )
    entities = client.get("/api/nodes", params={"q": "s"}).json()
    assert [n["label"] for n in entities] == ["SQLite"]
    types = client.get("/api/nodes", params={"q": "has", "kind": "edge_type"}).json()
    assert [n["label"] for n in types] == ["has_license"]


def test_ui_is_served_at_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "quick capture" in response.text
