"""The graph management interface: edge CRUD, the predicate vocabulary, and
replay stability for both."""

import pytest

from helpers import build_cf_html
from inspeg import service
from inspeg.adapters.clipboard import ClipboardSnapshot
from inspeg.store import events as ev


@pytest.fixture
def capture(store):
    snap = ClipboardSnapshot(
        cf_html=build_cf_html("<p>evidence</p>", source_url="https://e.test/"),
        text="evidence",
    )
    return service.ingest_clipboard(store, snap)


# ── predicate vocabulary ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("instance of", "INSTANCE_OF"),
        ("has-license", "HAS_LICENSE"),
        ("  part   of ", "PART_OF"),
        ("REL2", "REL2"),
    ],
)
def test_predicate_normalization(raw, normalized):
    assert service.normalize_predicate(raw) == normalized


@pytest.mark.parametrize("bad", ["", "   ", "é_ACCENT", "1STARTS_WITH_DIGIT", "A.B", "_LEADING"])
def test_invalid_predicates_are_rejected(bad):
    with pytest.raises(ValueError):
        service.normalize_predicate(bad)


def test_unknown_predicate_is_refused_without_create(store, capture):
    with pytest.raises(service.UnknownPredicateError):
        service.assert_edge(
            store, anchor_id=capture.anchor_id, src_label="A", edge_type="NEW_REL", dst_label="B"
        )
    assert store.query_one("SELECT COUNT(*) AS c FROM edge")["c"] == 0


def test_predicates_endpoint_lists_the_vocabulary(client):
    client.post("/api/predicates", json={"label": "cites"})
    client.post("/api/predicates", json={"label": "PART_OF"})
    labels = [p["label"] for p in client.get("/api/predicates").json()]
    assert labels == ["CITES", "PART_OF"]


def test_invalid_predicate_via_api_is_422(client):
    assert client.post("/api/predicates", json={"label": "no.dots"}).status_code == 422


def test_creating_a_predicate_twice_is_idempotent(store):
    first = service.create_predicate(store, "cites")
    second = service.create_predicate(store, "CITES")
    assert first == second


def test_replay_normalizes_legacy_lowercase_predicates(store):
    """Events recorded before the vocabulary existed carry lowercase labels;
    replay must project them into the normalized form."""
    with store.tx():
        store.record(
            ev.NODE_ASSERTED, {"id": "n_t", "label": "instance of", "props": {"kind": "edge_type"}}
        )
        store.record(ev.NODE_ASSERTED, {"id": "n_a", "label": "A", "props": {}})
        store.record(ev.NODE_ASSERTED, {"id": "n_b", "label": "B", "props": {}})
        store.record(
            ev.EDGE_ASSERTED,
            {"id": "e_1", "src": "n_a", "type": "instance of", "dst": "n_b", "props": {}},
        )
    store.replay()
    assert store.query_one("SELECT label FROM node WHERE id = 'n_t'")["label"] == "INSTANCE_OF"
    assert store.query_one("SELECT type FROM edge WHERE id = 'e_1'")["type"] == "INSTANCE_OF"
    # Entity labels are untouched.
    assert store.query_one("SELECT label FROM node WHERE id = 'n_a'")["label"] == "A"


# ── edge CRUD ────────────────────────────────────────────────────────────────


def seed_edge(client, capture, src="A", pred="REL", dst="B", note=None):
    response = client.post(
        "/api/edges",
        json={
            "anchor_id": capture.anchor_id,
            "src_label": src,
            "edge_type": pred,
            "dst_label": dst,
            "note": note,
            "create_predicate": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_edge_list_carries_labels_note_and_evidence(client, capture):
    seed_edge(client, capture, "SQLite", "HAS_LICENSE", "Public Domain", note="checked")
    manual = client.post(
        "/api/edges",
        json={"src_label": "X", "edge_type": "REL", "dst_label": "Y", "create_predicate": True},
    )
    assert manual.status_code == 200, manual.text

    rows = client.get("/api/edges").json()
    assert len(rows) == 2
    by_src = {row["src"]["label"]: row for row in rows}
    evidenced = by_src["SQLite"]
    assert evidenced["type"] == "HAS_LICENSE"
    assert evidenced["dst"]["label"] == "Public Domain"
    assert evidenced["note"] == "checked"
    assert evidenced["evidence"] == 1
    assert evidenced["anchor_id"] == capture.anchor_id
    # Manual entries are legitimate but visibly unevidenced.
    assert by_src["X"]["evidence"] == 0
    assert by_src["X"]["anchor_id"] is None


def test_delete_edge_removes_it_and_logs_the_retraction(client, capture, store):
    edge = seed_edge(client, capture)
    response = client.delete(f"/api/edges/{edge['id']}")
    assert response.status_code == 200
    assert client.get("/api/edges").json() == []
    assert store.query_one("SELECT * FROM edge WHERE id = ?", (edge["id"],)) is None
    assert store.query_one("SELECT * FROM support WHERE subject_id = ?", (edge["id"],)) is None
    event = store.query_one(
        "SELECT payload FROM event WHERE kind = 'edge_retracted' ORDER BY seq DESC LIMIT 1"
    )
    assert '"reason": "removed"' in event["payload"] or '"reason":"removed"' in event["payload"]


def test_delete_unknown_edge_is_404(client):
    assert client.delete("/api/edges/e_nope").status_code == 404


def test_edit_edge_carries_evidence_and_keeps_history(client, capture, store):
    original = seed_edge(client, capture, "MSFT", "REL", "Tech Company")
    response = client.put(
        f"/api/edges/{original['id']}",
        json={
            "src_label": "Microsoft Corp.",
            "edge_type": "INSTANCE_OF",
            "dst_label": "Tech Company",
            "create_predicate": True,
        },
    )
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["id"] != original["id"]  # corrections get a new id

    rows = client.get("/api/edges").json()
    assert len(rows) == 1
    assert rows[0]["src"]["label"] == "Microsoft Corp."
    assert rows[0]["type"] == "INSTANCE_OF"
    assert rows[0]["evidence"] == 1  # evidence carried over
    assert rows[0]["anchor_id"] == capture.anchor_id

    # The log records the correction, which is the valuable part.
    event = store.query_one(
        "SELECT payload FROM event WHERE kind = 'edge_retracted' ORDER BY seq DESC LIMIT 1"
    )
    assert "edited" in event["payload"]


def test_edit_with_unknown_predicate_rolls_back_the_retraction(client, capture):
    original = seed_edge(client, capture)
    response = client.put(
        f"/api/edges/{original['id']}",
        json={"src_label": "A", "edge_type": "NOT_IN_VOCAB", "dst_label": "B"},
    )
    assert response.status_code == 422
    # The original edge must still exist: retract + failed re-assert is atomic.
    assert [row["id"] for row in client.get("/api/edges").json()] == [original["id"]]


def test_manual_edge_without_anchor(store):
    result = service.assert_edge(
        store, src_label="A", edge_type="REL", dst_label="B", create_predicate=True
    )
    assert result["anchor_id"] is None
    assert store.query_one("SELECT COUNT(*) AS c FROM support")["c"] == 0


def test_replay_is_stable_after_edits_and_deletes(client, capture, store):
    first = seed_edge(client, capture, "A", "REL", "B")
    second = seed_edge(client, capture, "C", "REL", "D")
    client.put(
        f"/api/edges/{first['id']}",
        json={"src_label": "A2", "edge_type": "REL", "dst_label": "B2"},
    )
    client.delete(f"/api/edges/{second['id']}")

    def snapshot():
        return {
            table: [tuple(row) for row in store.query(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("node", "edge", "support")
        }

    before = snapshot()
    store.replay()
    assert snapshot() == before
    assert len(client.get("/api/edges").json()) == 1
