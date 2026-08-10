"""FTS cache (Phase 7): ephemeral, redaction-safe, rebuildable."""

import pytest
from fastapi.testclient import TestClient

from conftest import BASE_URL
from inspeg import service
from inspeg.api.app import create_app
from inspeg.fts import FtsIndex

CAPTURE_HEADERS = {"X-Inspeg-Capture": "1"}


@pytest.fixture
def fts(store, tmp_path):
    index = FtsIndex(store, tmp_path / "cache.db", start_worker=False)
    store.on_commit.append(index.on_commit)
    yield index
    index.close()


def capture_doc(store, text="The mitochondria is the powerhouse of the cell"):
    return service.ingest_web_capture(
        store,
        url="https://bio.example/article",
        doc_text=text,
        selection_exact=text.split()[0],
    )


def test_capture_is_searchable_after_drain(store, fts):
    result = capture_doc(store)
    fts.process_pending()
    hits = fts.search("mitochondria")
    assert hits
    assert hits[0]["artifact_id"] == result["artifact_id"]
    assert "<<mitochondria>>" in hits[0]["snippet"]


def test_redaction_deletes_from_fts_synchronously(store, fts):
    result = capture_doc(store)
    fts.process_pending()
    assert fts.search("mitochondria")
    service.redact_artifact(store, result["artifact_id"])
    # No drain: the deletion must have happened inside the commit callback,
    # before redact_artifact returned (ADR 0002 — every content copy dies).
    assert fts.search("mitochondria") == []


def test_reopen_reconciles_missed_redactions(store, tmp_path):
    """Crash backstop: a redaction recorded while no index was attached must
    be purged when the index next opens."""
    index = FtsIndex(store, tmp_path / "cache.db", start_worker=False)
    store.on_commit.append(index.on_commit)
    result = capture_doc(store)
    index.process_pending()
    store.on_commit.clear()  # simulate the indexer being gone
    index.close()
    service.redact_artifact(store, result["artifact_id"])
    reopened = FtsIndex(store, tmp_path / "cache.db", start_worker=False)
    assert reopened.search("mitochondria") == []
    reopened.close()


def test_rebuild_skips_redacted_and_pointers(store, fts):
    keep = capture_doc(store, "alpha document text")
    gone = capture_doc(store, "beta document text")
    service.capture_pointer(store, kind="url", target="https://x.com/img.png", surface="browser")
    service.redact_artifact(store, gone["artifact_id"])
    count = fts.rebuild()
    assert count >= 1
    assert fts.search("alpha")[0]["artifact_id"] == keep["artifact_id"]
    assert fts.search("beta") == []


def test_bad_query_is_a_value_error(store, fts):
    with pytest.raises(ValueError):
        fts.search('"unbalanced')


def test_search_endpoint(store, tmp_path):
    index = FtsIndex(store, tmp_path / "cache.db", start_worker=False)
    store.on_commit.append(index.on_commit)
    client = TestClient(create_app(store, fts=index), base_url=BASE_URL)
    client.post(
        "/api/captures/selection",
        json={
            "url": "https://bio.example/article",
            "doc_text": "The mitochondria is the powerhouse of the cell",
            "selection_exact": "powerhouse",
        },
        headers=CAPTURE_HEADERS,
    )
    index.process_pending()
    result = client.get("/api/search", params={"q": "powerhouse"}).json()
    assert result["items"]
    assert "powerhouse" in result["items"][0]["item"]["excerpt"]
    assert client.get("/api/search", params={"q": '"broken'}).status_code == 422
    index.close()


def test_search_disabled_without_index(client):
    assert client.get("/api/search", params={"q": "x"}).status_code == 503
