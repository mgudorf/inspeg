import json

from inspeg.store import events as ev

PROJECTION_TABLES = ("artifact", "anchor", "node", "node_alias", "edge", "support")


def dump_projection(store):
    return {
        table: [tuple(row) for row in store.query(f"SELECT * FROM {table} ORDER BY 1")]
        for table in PROJECTION_TABLES
    }


def test_record_appends_event_and_updates_projection(store):
    with store.tx():
        store.record(ev.NODE_ASSERTED, {"id": "n_1", "label": "Microsoft", "props": {}})
    node = store.query_one("SELECT * FROM node WHERE id = 'n_1'")
    assert node["label"] == "Microsoft"
    alias = store.query_one("SELECT * FROM node_alias WHERE node_id = 'n_1'")
    assert alias["surface"] == "Microsoft"
    event = store.query_one("SELECT * FROM event ORDER BY seq DESC LIMIT 1")
    assert event["kind"] == ev.NODE_ASSERTED
    assert event["actor"] == "human"
    assert json.loads(event["payload"])["label"] == "Microsoft"


def test_actor_is_recorded(store):
    with store.tx():
        store.record(
            ev.NODE_ASSERTED, {"id": "n_2", "label": "X", "props": {}}, actor="proposer:demo"
        )
    event = store.query_one("SELECT actor FROM event ORDER BY seq DESC LIMIT 1")
    assert event["actor"] == "proposer:demo"


def test_unknown_event_kind_is_tolerated(store):
    with store.tx():
        store.record("future_event_kind", {"whatever": 1})
    assert store.query_one("SELECT COUNT(*) AS c FROM event")["c"] == 1


def test_replay_rebuilds_identical_projection(store):
    with store.tx():
        store.record(ev.NODE_ASSERTED, {"id": "n_a", "label": "A", "props": {}})
        store.record(ev.NODE_ASSERTED, {"id": "n_b", "label": "B", "props": {}})
        store.record(
            ev.EDGE_ASSERTED,
            {"id": "e_1", "src": "n_a", "type": "relates_to", "dst": "n_b", "props": {}},
        )
    before = dump_projection(store)
    replayed = store.replay()
    assert replayed == 3
    assert dump_projection(store) == before
