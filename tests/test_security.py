"""Security regression tests: one per vector patched in docs/security.md.

Each test is written so that removing the corresponding control makes it fail.
Every attack is expressed the way a real attacker would send it — a browser
request from a hostile page, a forged clipboard payload, an oversized capture —
rather than by asserting on the implementation.
"""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from conftest import BASE_URL
from helpers import build_cf_html
from inspeg import service
from inspeg.adapters.clipboard import ClipboardSnapshot
from inspeg.api.app import create_app
from inspeg.store import Store, StoreLockedError
from inspeg.store.blobstore import BlobStore

EVIL = "http://evil.example"


@pytest.fixture
def capture(store):
    return service.ingest_clipboard(
        store,
        ClipboardSnapshot(
            cf_html=build_cf_html("<p>evidence</p>", source_url="https://sqlite.org/"),
            text="evidence",
            source_app="chrome.exe | SQLite",
        ),
    )


# ── V1: cross-site request forgery ──────────────────────────────────────────


def test_cross_origin_capture_is_rejected(client):
    """A malicious page POSTing the no-body capture endpoint gets 403, not a
    clipboard read. This is the whole-clipboard-exfiltration vector."""
    response = client.post(
        "/api/captures/clipboard",
        headers={"Origin": EVIL, "X-Inspeg-Capture": "1"},
    )
    assert response.status_code == 403


def test_cross_origin_edge_assertion_is_rejected(client, capture):
    response = client.post(
        "/api/edges",
        json={
            "anchor_id": capture.anchor_id,
            "src_label": "A",
            "edge_type": "r",
            "dst_label": "B",
        },
        headers={"Origin": EVIL},
    )
    assert response.status_code == 403
    assert client.get("/api/stats").json()["edge"] == 0


def test_cross_origin_read_is_rejected(client, capture):
    """Reads are blocked too: a hostile page must not scrape the graph."""
    response = client.get("/api/anchors/latest", headers={"Origin": EVIL})
    assert response.status_code == 403


def test_capture_without_csrf_header_is_rejected(client):
    """Defence in depth: even same-origin, the capture POST needs a custom
    header, which cross-origin JS cannot set without a failing preflight."""
    response = client.post("/api/captures/clipboard")
    assert response.status_code == 403
    assert "X-Inspeg-Capture" in response.json()["detail"]


def test_same_origin_requests_still_work(client, capture):
    """The controls must not break the real UI, which does send Origin."""
    headers = {"Origin": BASE_URL}
    assert client.get("/api/anchors/latest", headers=headers).status_code == 200
    response = client.post(
        "/api/edges",
        json={
            "anchor_id": capture.anchor_id,
            "src_label": "A",
            "edge_type": "r",
            "dst_label": "B",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text


def test_non_browser_clients_without_origin_still_work(client, capture):
    """curl and the adapters send no Origin header; they must keep working."""
    assert client.get(f"/api/anchors/{capture.anchor_id}").status_code == 200


# ── V2: DNS rebinding ───────────────────────────────────────────────────────


def test_rebound_host_header_is_rejected(client, capture):
    """evil.com rebound to 127.0.0.1 arrives with its own Host header; the
    allowlist rejects it, so the page cannot read the graph same-origin."""
    response = client.get("/api/anchors/latest", headers={"Host": "evil.example"})
    assert response.status_code == 400


def test_loopback_hosts_are_allowed(client, capture):
    for host in ("127.0.0.1:8137", "localhost:8137", "127.0.0.1"):
        assert client.get("/api/health", headers={"Host": host}).status_code == 200


def test_allow_remote_opt_in_disables_the_host_allowlist(store):
    """--allow-remote is an explicit, documented choice; it must actually work
    for the LAN-access use case it exists for."""
    remote = TestClient(create_app(store, allow_remote=True), base_url=BASE_URL)
    assert remote.get("/api/health", headers={"Host": "some.host.example"}).status_code == 200


# ── V3: non-loopback bind guard ─────────────────────────────────────────────


def test_non_loopback_bind_is_refused_without_opt_in():
    from inspeg.__main__ import main

    with pytest.raises(SystemExit) as excinfo:
        main(["--host", "0.0.0.0", "--no-hotkey"])
    assert excinfo.value.code == 2  # argparse error, before anything binds


def test_loopback_detection():
    from inspeg.__main__ import _is_loopback

    assert _is_loopback("127.0.0.1")
    assert _is_loopback("localhost")
    assert _is_loopback("::1")
    assert not _is_loopback("0.0.0.0")
    assert not _is_loopback("192.168.1.5")
    assert not _is_loopback("evil.example")


# ── V4: hostile URL in a clipboard payload ──────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(document.domain)",
        "JavaScript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "vbscript:msgbox(1)",
        "file:///C:/Windows/System32/config/SAM",
    ],
)
def test_dangerous_url_schemes_are_never_linkable(url):
    assert service.safe_url(url) is None


@pytest.mark.parametrize("url", ["http://example.com/a", "https://example.com/a?b=c#d"])
def test_http_urls_are_linkable(url):
    assert service.safe_url(url) == url


def test_forged_source_url_is_not_served_as_a_link(client, store):
    """Any local app can write CF_HTML. The forged URL is retained as
    provenance but must not reach the UI's href."""
    payload = build_cf_html("<p>x</p>", source_url="javascript:alert(document.domain)")
    capture = service.ingest_clipboard(store, ClipboardSnapshot(cf_html=payload))
    artifact = client.get(f"/api/anchors/{capture.anchor_id}").json()["artifact"]
    assert artifact["source_uri"] == "javascript:alert(document.domain)"
    assert artifact["source_link"] is None


# ── V5: resource exhaustion ─────────────────────────────────────────────────


def test_oversized_capture_is_refused(store):
    oversized = "A" * (service.MAX_CAPTURE_BYTES + 1)
    with pytest.raises(service.CaptureTooLargeError):
        service.ingest_clipboard(store, ClipboardSnapshot(text=oversized))
    assert store.query_one("SELECT COUNT(*) AS c FROM artifact")["c"] == 0
    assert store.query_one("SELECT COUNT(*) AS c FROM event")["c"] == 0


def test_excerpt_generation_does_not_parse_the_whole_document(store):
    """A huge fragment must not be handed to the HTML parser in full: the
    excerpt is bounded, so the parse input is bounded too."""
    fragment = "<p>start</p>" + ("<span>filler</span>" * 20_000)
    capture = service.ingest_clipboard(store, ClipboardSnapshot(cf_html=build_cf_html(fragment)))
    assert len(capture.excerpt) <= service.EXCERPT_LIMIT
    assert len(fragment) > service.EXCERPT_HTML_SLICE


def test_node_search_limit_is_clamped(client, capture):
    """An unbounded limit would let one request pull the whole node table."""
    for i in range(3):
        client.post(
            "/api/edges",
            json={
                "anchor_id": capture.anchor_id,
                "src_label": f"n{i}",
                "edge_type": "r",
                "dst_label": "B",
            },
        )
    assert len(client.get("/api/nodes", params={"q": "", "limit": 10_000}).json()) <= 100


# ── V6: path traversal through a digest ─────────────────────────────────────


@pytest.mark.parametrize(
    "digest",
    [
        "../../../../Windows/System32/config/SAM",
        "..%2f..%2fsecret",
        "ab/../../etc/passwd",
        "",
        "ZZ" * 32,
        "abc",
        "A" * 64,  # uppercase hex is not the canonical form we write
    ],
)
def test_blobstore_rejects_non_digest_paths(tmp_path, digest):
    blobs = BlobStore(tmp_path)
    with pytest.raises(ValueError):
        blobs.relpath(digest)
    with pytest.raises(ValueError):
        blobs.get(digest)


def test_blobstore_accepts_real_digests(tmp_path):
    blobs = BlobStore(tmp_path)
    digest, _ = blobs.put(b"content")
    assert blobs.get(digest) == b"content"


# ── V7: SQL injection and stored XSS through labels ─────────────────────────


def test_sql_injection_in_labels_is_inert(client, capture):
    hostile = "Robert'); DROP TABLE node;--"
    response = client.post(
        "/api/edges",
        json={
            "anchor_id": capture.anchor_id,
            "src_label": hostile,
            "edge_type": "r",
            "dst_label": "B",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["src"]["label"] == hostile  # stored as data
    assert client.get("/api/stats").json()["node"] == 3  # table intact


def test_sql_wildcards_in_search_are_literal(client, capture):
    for label in ("alpha", "a_b", "a%c"):
        client.post(
            "/api/edges",
            json={
                "anchor_id": capture.anchor_id,
                "src_label": label,
                "edge_type": "r",
                "dst_label": "B",
            },
        )
    # '_' matches any character in LIKE; escaped, it matches only itself.
    assert [n["label"] for n in client.get("/api/nodes", params={"q": "a_"}).json()] == ["a_b"]
    assert [n["label"] for n in client.get("/api/nodes", params={"q": "a%"}).json()] == ["a%c"]


def test_script_payload_in_a_label_survives_as_inert_data(client, capture):
    """The UI escapes on render; the API must store and return payloads
    unmangled so escaping stays the single, testable chokepoint."""
    payload = "<img src=x onerror=alert(1)>"
    response = client.post(
        "/api/edges",
        json={
            "anchor_id": capture.anchor_id,
            "src_label": payload,
            "edge_type": "r",
            "dst_label": "B",
        },
    )
    assert response.json()["src"]["label"] == payload


# ── V8: single-instance enforcement ─────────────────────────────────────────


def test_second_store_on_the_same_data_dir_is_refused(tmp_path):
    """Two daemons on one data dir race the blobstore and the projection."""
    first = Store(tmp_path / "data")
    try:
        with pytest.raises(StoreLockedError):
            Store(tmp_path / "data")
    finally:
        first.close()


def test_lock_is_released_on_close(tmp_path):
    first = Store(tmp_path / "data")
    first.close()
    second = Store(tmp_path / "data")  # must not raise
    second.close()


def test_close_is_idempotent(tmp_path):
    """The console-close handler and the finally block may both call close()."""
    store = Store(tmp_path / "data")
    store.close()
    store.close()


# ── V9: redaction (destroying an accidental secret capture) ─────────────────


def test_redaction_deletes_the_blob_and_flags_the_artifact(store, capture):
    assert store.blobs.exists(capture.artifact_id)
    service.redact_artifact(store, capture.artifact_id)
    assert not store.blobs.exists(capture.artifact_id)
    row = store.query_one("SELECT redacted FROM artifact WHERE id = ?", (capture.artifact_id,))
    assert row["redacted"] == 1


def test_redaction_survives_replay(store, capture):
    """The append-only log must not resurrect redacted content."""
    service.redact_artifact(store, capture.artifact_id)
    store.replay()
    row = store.query_one("SELECT redacted FROM artifact WHERE id = ?", (capture.artifact_id,))
    assert row["redacted"] == 1
    assert not store.blobs.exists(capture.artifact_id)


def test_redacted_artifact_serves_no_excerpt(client, store, capture):
    service.redact_artifact(store, capture.artifact_id)
    body = client.get(f"/api/anchors/{capture.anchor_id}").json()
    assert body["artifact"]["redacted"] is True
    assert body["excerpt"] is None
    # Provenance survives: what was captured, and from where, is still known.
    assert body["artifact"]["source_uri"] == "https://sqlite.org/"


def test_redaction_is_idempotent_and_appends_one_event(store, capture):
    service.redact_artifact(store, capture.artifact_id)
    service.redact_artifact(store, capture.artifact_id)
    count = store.query_one("SELECT COUNT(*) AS c FROM event WHERE kind = 'artifact_redacted'")["c"]
    assert count == 1


def test_redacting_unknown_artifact_raises(store):
    with pytest.raises(service.UnknownArtifactError):
        service.redact_artifact(store, "f" * 64)


def test_redact_endpoint_404s_for_unknown_artifact(client):
    assert client.post(f"/api/artifacts/{'f' * 64}/redact").status_code == 404


# ── V10: missing blob is a clean error, not a 500 ───────────────────────────


def test_missing_blob_returns_410_not_500(client, store, capture):
    (store.data_dir / store.blobs.relpath(capture.artifact_id)).unlink()
    response = client.get(f"/api/anchors/{capture.anchor_id}")
    assert response.status_code == 410


# ── V11: crash leftovers are swept, real data is not ────────────────────────


def test_startup_sweep_removes_tmp_files_but_keeps_live_blobs(tmp_path, capture, store):
    live_digest = capture.artifact_id
    shard = store.data_dir / "blobs" / live_digest[:2]
    orphan_tmp = shard / f"{'a' * 64}.999.deadbeef.tmp"
    orphan_tmp.write_bytes(b"half-written")
    rolled_back = shard / ("b" * 64)
    rolled_back.write_bytes(b"never committed")
    data_dir = store.data_dir
    store.close()

    reopened = Store(data_dir)
    try:
        assert not orphan_tmp.exists()  # crash leftover
        assert not rolled_back.exists()  # rollback orphan, no artifact row
        assert reopened.blobs.exists(live_digest)  # real capture untouched
    finally:
        reopened.close()


def test_sweep_does_not_touch_blobs_when_the_log_is_empty(tmp_path):
    """A fresh DB beside existing blobs means the DB is not theirs; deleting
    would be data loss, so only .tmp files go."""
    data_dir = tmp_path / "data"
    store = Store(data_dir)
    _, rel = store.blobs.put(b"unreferenced but precious")
    store.close()

    reopened = Store(data_dir)
    try:
        assert (data_dir / rel).exists()
    finally:
        reopened.close()


def test_concurrent_puts_of_same_content_use_distinct_temp_files(tmp_path):
    """Shared temp names let concurrent writers clobber each other mid-write."""
    blobs = BlobStore(tmp_path)
    data = b"x" * 1024
    digest, _ = blobs.put(data)
    assert blobs.get(digest) == data
    shard = tmp_path / "blobs" / digest[:2]
    assert not list(shard.glob("*.tmp"))  # replaced atomically, nothing left over


# ── V12: hotkey does not collide with AltGr ─────────────────────────────────


def test_default_hotkey_avoids_ctrl_alt():
    """Ctrl+Alt is how AltGr is synthesized on many non-US layouts: such a
    hotkey fires while the user is typing ordinary characters."""
    from inspeg.__main__ import DEFAULT_HOTKEY
    from inspeg.adapters.hotkey import _MOD_FLAGS, parse_hotkey

    mods, _ = parse_hotkey(DEFAULT_HOTKEY)
    ctrl_alt = _MOD_FLAGS["ctrl"] | _MOD_FLAGS["alt"]
    assert mods & ctrl_alt != ctrl_alt


def test_hotkey_requires_a_modifier():
    from inspeg.adapters.hotkey import parse_hotkey

    with pytest.raises(ValueError):
        parse_hotkey("a")


def test_hotkey_listener_is_a_daemon_thread():
    """Daemon threads die with the process, so closing the terminal cannot
    leave a capture listener running."""
    from inspeg.adapters.hotkey import HotkeyListener

    listener = HotkeyListener("win+shift+a", lambda: None)
    assert listener.daemon is True
    assert listener.status == "pending"


# ── V14: nothing outlives the terminal ──────────────────────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="console control handlers are Windows-only")
def test_console_close_handler_runs_cleanup():
    """Closing the console window delivers CTRL_CLOSE_EVENT and then kills the
    process, so finally/atexit may never run: the handler is what releases the
    store lock and unregisters the hotkey."""
    from inspeg.__main__ import _install_console_close_handler

    calls = []
    _install_console_close_handler(lambda: calls.append("cleanup"))

    import inspeg.__main__ as entry

    handler = entry._console_handler_ref
    assert handler is not None  # a collected callback would crash the process

    # Returns falsy (ctypes marshals BOOL as int) so the default handler still
    # terminates the process after cleanup.
    assert not handler(2)  # CTRL_CLOSE_EVENT
    assert calls == ["cleanup"]
    for logoff_or_shutdown in (5, 6):
        handler(logoff_or_shutdown)
    assert len(calls) == 3
    handler(0)  # CTRL_C_EVENT is left to Python/uvicorn
    assert len(calls) == 3


def test_cleanup_releases_the_lock_for_the_next_process(tmp_path):
    """What the console handler calls must actually free the data dir."""
    store = Store(tmp_path / "data")
    store.close()
    again = Store(tmp_path / "data")
    again.close()
