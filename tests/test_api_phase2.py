"""Phase-2 API core: read routes, pagination, labels routes, post-commit bus."""

import pytest

from inspeg import service
from inspeg.store import events as ev

CAPTURE_HEADERS = {"X-Inspeg-Capture": "1"}


def selection_body(**overrides):
    body = {
        "url": "https://example.com/article",
        "title": "An Article",
        "doc_text": "Alpha beta gamma delta",
        "selection_exact": "beta gamma",
        "selection_prefix": "Alpha ",
        "selection_suffix": " delta",
        "selection_start": 6,
        "selection_end": 16,
        "labels": ["AI Knowledge"],
    }
    body.update(overrides)
    return body


# ── capture routes ──────────────────────────────────────────────────────────


def test_selection_capture_roundtrip(client):
    response = client.post(
        "/api/captures/selection", json=selection_body(), headers=CAPTURE_HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provenance"] == "exact"
    assert body["labels"][0]["label"] == "AI Knowledge"
    detail = client.get(f"/api/anchors/{body['anchor_id']}").json()
    assert detail["anchor"]["selector_type"] == "text_quote"


def test_pointer_capture_route(client):
    response = client.post(
        "/api/captures/pointer",
        json={
            "kind": "url",
            "target": "https://example.com/img.png",
            "mimetype": "image/png",
            "page_uri": "https://example.com/article",
            "labels": ["Diagrams"],
        },
        headers=CAPTURE_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["artifact_id"].startswith("pt_")


def test_code_capture_route(client, tmp_path):
    response = client.post(
        "/api/captures/code",
        json={
            "text": "x = 1",
            "path": str(tmp_path / "m.py"),
            "start_line": 3,
            "start_col": 0,
            "end_line": 3,
            "end_col": 5,
            "workspace": str(tmp_path),
            "labels": ["Code Patterns"],
        },
        headers=CAPTURE_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["provenance"] == "exact"


def test_capture_routes_require_header(client):
    for path, body in (
        ("/api/captures/selection", selection_body()),
        ("/api/captures/pointer", {"kind": "url", "target": "https://x.com/a"}),
    ):
        assert client.post(path, json=body).status_code == 403


# ── labels routes ───────────────────────────────────────────────────────────


def test_label_apply_list_remove_roundtrip(client):
    captured = client.post(
        "/api/captures/selection", json=selection_body(labels=[]), headers=CAPTURE_HEADERS
    ).json()
    anchor_id = captured["anchor_id"]
    applied = client.post(
        f"/api/anchors/{anchor_id}/labels", json={"label": "Bias"}, headers=CAPTURE_HEADERS
    )
    assert applied.status_code == 200
    labels = client.get("/api/labels?sort=recent").json()
    assert any(entry["label"] == "Bias" for entry in labels)
    removed = client.delete(
        f"/api/anchors/{anchor_id}/labels", params={"label": "Bias"}, headers=CAPTURE_HEADERS
    )
    assert removed.status_code == 200
    labels = client.get("/api/labels?sort=recent").json()
    assert not any(entry["label"] == "Bias" for entry in labels)


# ── read routes ─────────────────────────────────────────────────────────────


def test_resolve_by_url(client):
    client.post("/api/captures/selection", json=selection_body(), headers=CAPTURE_HEADERS)
    result = client.get(
        "/api/resolve", params={"url": "https://example.com/article#section"}
    ).json()
    assert result["context_key"] == "url:https://example.com/article"
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["labels"][0]["label"] == "AI Knowledge"
    assert item["excerpt"].startswith("Alpha beta")


def test_resolve_requires_exactly_one_selector(client):
    assert client.get("/api/resolve").status_code == 422
    assert (
        client.get("/api/resolve", params={"url": "https://x.com", "exe": "a.exe"}).status_code
        == 422
    )


def test_tree_groups_by_context_and_label(client):
    client.post("/api/captures/selection", json=selection_body(), headers=CAPTURE_HEADERS)
    by_context = client.get("/api/tree", params={"group_by": "context"}).json()
    assert any(g["kind"] == "url" for g in by_context["groups"])
    by_label = client.get("/api/tree", params={"group_by": "label"}).json()
    assert any(g["display"] == "AI Knowledge" for g in by_label["groups"])


def test_label_items_and_similar(client, store):
    first = client.post(
        "/api/captures/selection", json=selection_body(), headers=CAPTURE_HEADERS
    ).json()
    second = client.post(
        "/api/captures/selection",
        json=selection_body(url="https://other.example/two", doc_text="Entirely different text"),
        headers=CAPTURE_HEADERS,
    ).json()
    label_id = first["labels"][0]["id"]
    items = client.get(f"/api/labels/{label_id}/items").json()
    assert len(items["items"]) == 2
    similar = client.get(f"/api/items/{first['anchor_id']}/similar").json()
    assert similar["items"][0]["anchor_id"] == second["anchor_id"]
    assert similar["items"][0]["shared"] == 1


def test_url_digests_lists_normalized_urls_only(client, store):
    client.post("/api/captures/selection", json=selection_body(), headers=CAPTURE_HEADERS)
    digests = client.get("/api/anchors/url-digests").json()["digests"]
    import hashlib

    expected = hashlib.sha256(b"https://example.com/article").hexdigest()
    assert expected in digests


def test_edges_pagination_cursor_header(client, store):
    anchor = client.post(
        "/api/captures/selection", json=selection_body(labels=[]), headers=CAPTURE_HEADERS
    ).json()["anchor_id"]
    client.post("/api/predicates", json={"label": "RELATES_TO"})
    for i in range(3):
        client.post(
            "/api/edges",
            json={
                "anchor_id": anchor,
                "src_label": f"N{i}",
                "edge_type": "RELATES_TO",
                "dst_label": f"M{i}",
            },
        )
    page = client.get("/api/edges", params={"limit": 2})
    assert len(page.json()) == 2
    cursor = page.headers["X-Next-Cursor"]
    rest = client.get("/api/edges", params={"limit": 2, "cursor": cursor})
    assert len(rest.json()) == 1
    assert "X-Next-Cursor" not in rest.headers


# ── post-commit bus ─────────────────────────────────────────────────────────


def test_on_commit_fires_once_per_tx_with_all_events(store):
    received = []
    store.on_commit.append(received.append)
    service.capture_pointer(store, kind="url", target="https://x.com/a", surface="hud")
    assert len(received) == 1
    kinds = [e["kind"] for e in received[0]]
    assert ev.ARTIFACT_ADDED in kinds
    assert ev.ANCHOR_ADDED in kinds


def test_on_commit_suppressed_on_rollback(store):
    received = []
    store.on_commit.append(received.append)
    with pytest.raises(RuntimeError), store.tx():
        store.record(ev.NODE_ASSERTED, {"id": "n_x", "label": "X", "props": {}})
        raise RuntimeError("boom")
    assert received == []
    assert store.query_one("SELECT 1 FROM node WHERE id = 'n_x'") is None


def test_events_stream_connects(client):
    # ttl=0 yields the connected comment and closes — a finite stream, so the
    # test never has to early-close an infinite generator.
    response = client.get("/api/events/stream", params={"ttl": 0})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "connected" in response.text


# ── HUD support routes (Phase 6) ────────────────────────────────────────────


def test_queue_lists_only_unlabeled_captures(client):
    labeled = client.post(
        "/api/captures/selection", json=selection_body(), headers=CAPTURE_HEADERS
    ).json()
    unlabeled = client.post(
        "/api/captures/selection",
        json=selection_body(
            url="https://plain.example/x", doc_text="Unlabeled content here", labels=[]
        ),
        headers=CAPTURE_HEADERS,
    ).json()
    queue = client.get("/api/queue").json()["items"]
    ids = [item["artifact"]["id"] for item in queue]
    assert unlabeled["artifact_id"] in ids
    assert labeled["artifact_id"] not in ids


def test_resolve_by_context_key_expands_tree_groups(client, tmp_path):
    client.post(
        "/api/captures/code",
        json={
            "text": "y = 2",
            "path": str(tmp_path / "n.py"),
            "start_line": 1,
            "start_col": 0,
            "end_line": 1,
            "end_col": 5,
            "workspace": str(tmp_path),
            "labels": [],
        },
        headers=CAPTURE_HEADERS,
    )
    groups = client.get("/api/tree", params={"group_by": "context"}).json()["groups"]
    workspace_group = next(g for g in groups if g["kind"] == "workspace")
    resolved = client.get("/api/resolve", params={"key": workspace_group["key"]}).json()
    assert len(resolved["items"]) == 1
    assert resolved["items"][0]["artifact"]["provenance"] == "exact"
