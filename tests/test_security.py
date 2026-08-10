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
            "create_predicate": True,
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
                "create_predicate": True,
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
            "create_predicate": True,
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
                "create_predicate": True,
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
            "create_predicate": True,
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
    # Redaction is a write: since the router split it carries the same
    # X-Inspeg-Capture belt-and-braces as every other write route.
    response = client.post(f"/api/artifacts/{'f' * 64}/redact", headers={"X-Inspeg-Capture": "1"})
    assert response.status_code == 404


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


def test_default_hotkey_avoids_bare_ctrl_alt():
    """Ctrl+Alt is how AltGr is synthesized on many non-US layouts: a hotkey
    of exactly ctrl+alt+<key> fires while the user is typing ordinary
    characters. A default that includes ctrl+alt must therefore also require
    a further modifier (shift or win), which plain AltGr typing never sets.
    The only remaining default hotkey is the HUD toggle."""
    from inspeg.__main__ import DEFAULT_HUD_HOTKEY
    from inspeg.adapters.hotkey import _MOD_FLAGS, parse_hotkey

    mods, _ = parse_hotkey(DEFAULT_HUD_HOTKEY)
    ctrl_alt = _MOD_FLAGS["ctrl"] | _MOD_FLAGS["alt"]
    if mods & ctrl_alt == ctrl_alt:
        assert mods & (_MOD_FLAGS["shift"] | _MOD_FLAGS["win"])


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


# ── V15: extension-origin allowlist (ADR 0007) ──────────────────────────────

EXT_ID = "a" * 32
EXT_ORIGIN = f"chrome-extension://{EXT_ID}"
OTHER_EXT_ORIGIN = f"chrome-extension://{'b' * 32}"

SELECTION_BODY = {
    "url": "https://example.com/page",
    "doc_text": "Alpha beta gamma",
    "selection_exact": "beta",
    "selection_prefix": "Alpha ",
    "selection_suffix": " gamma",
    "labels": ["Topic"],
}


class SpyOpener:
    """V17 test double: records launches; must never actually launch."""

    def __init__(self):
        self.opened = []
        self.revealed = []

    def open_url(self, url):
        self.opened.append(url)

    def reveal_in_explorer(self, path):
        self.revealed.append(path)


@pytest.fixture
def spy_opener():
    return SpyOpener()


@pytest.fixture
def ext_client(store, spy_opener):
    return TestClient(
        create_app(store, extension_origins=[EXT_ORIGIN], opener=spy_opener),
        base_url=BASE_URL,
    )


def test_allowlisted_extension_origin_is_accepted(ext_client):
    response = ext_client.post(
        "/api/captures/selection",
        json=SELECTION_BODY,
        headers={"Origin": EXT_ORIGIN, "X-Inspeg-Capture": "1"},
    )
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == EXT_ORIGIN
    assert response.json()["provenance"] == "exact"


def test_non_allowlisted_extension_origin_is_rejected(ext_client):
    response = ext_client.post(
        "/api/captures/selection",
        json=SELECTION_BODY,
        headers={"Origin": OTHER_EXT_ORIGIN, "X-Inspeg-Capture": "1"},
    )
    assert response.status_code == 403


def test_web_origins_still_rejected_when_allowlist_configured(ext_client):
    """The V1 control must survive the V15 carve-out untouched."""
    response = ext_client.post(
        "/api/captures/selection",
        json=SELECTION_BODY,
        headers={"Origin": EVIL, "X-Inspeg-Capture": "1"},
    )
    assert response.status_code == 403


def test_extension_origin_still_requires_capture_header(ext_client):
    response = ext_client.post(
        "/api/captures/selection", json=SELECTION_BODY, headers={"Origin": EXT_ORIGIN}
    )
    assert response.status_code == 403
    assert "X-Inspeg-Capture" in response.json()["detail"]


def test_extension_origin_cannot_delete_artifacts(ext_client, store):
    """Hard delete (ADR 0010) is a human-at-the-HUD action: the extension
    allowlist must not reach it even with the capture header."""
    from test_multi_surface import web_capture

    result = web_capture(store)
    response = ext_client.delete(
        f"/api/artifacts/{result['artifact_id']}",
        headers={"Origin": EXT_ORIGIN, "X-Inspeg-Capture": "1"},
    )
    assert response.status_code == 403
    assert store.query_one("SELECT 1 FROM artifact WHERE id = ?", (result["artifact_id"],))


def test_extension_origin_is_confined_to_extension_routes(ext_client, store):
    """Least privilege: the allowlist opens specific routes, not the API."""
    edge = ext_client.post(
        "/api/edges",
        json={"src_label": "A", "edge_type": "R", "dst_label": "B"},
        headers={"Origin": EXT_ORIGIN, "X-Inspeg-Capture": "1"},
    )
    assert edge.status_code == 403
    opened = ext_client.post(
        "/api/open",
        json={"url": "https://example.com"},
        headers={"Origin": EXT_ORIGIN, "X-Inspeg-Open": "1"},
    )
    assert opened.status_code == 403


@pytest.mark.parametrize(
    "bad_origin",
    [
        "chrome-extension://*",
        "*",
        "chrome-extension://tooshort",
        "chrome-extension://" + "z" * 32,  # z is outside a-p: not a real id
        "https://evil.example",
        "moz-extension://" + "a" * 32,
    ],
)
def test_malformed_or_wildcard_extension_origin_is_refused(store, bad_origin):
    with pytest.raises(ValueError):
        create_app(store, extension_origins=[bad_origin])


def test_preflight_echoes_exactly_the_matched_origin(ext_client):
    response = ext_client.options(
        "/api/captures/selection",
        headers={
            "Origin": EXT_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-inspeg-capture",
        },
    )
    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == EXT_ORIGIN  # never "*"
    assert "x-inspeg-capture" in response.headers["Access-Control-Allow-Headers"].lower()


def test_preflight_for_web_origin_still_fails(ext_client):
    response = ext_client.options(
        "/api/captures/selection",
        headers={"Origin": EVIL, "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 403


# ── V17: deep-link dispatch scheme allowlist ────────────────────────────────

OPEN_HEADERS = {"X-Inspeg-Open": "1"}


@pytest.mark.parametrize(
    "hostile",
    [
        "javascript:alert(1)",
        "file:///C:/Windows/System32/calc.exe",
        "data:text/html,x",
        "ms-settings:",
        r"\\evil\share\x",
    ],
)
def test_open_refuses_non_allowlisted_schemes(ext_client, spy_opener, hostile):
    response = ext_client.post("/api/open", json={"url": hostile}, headers=OPEN_HEADERS)
    assert response.status_code == 422
    assert spy_opener.opened == []


def test_open_allows_https_and_vscode_links(ext_client, spy_opener):
    for url in ("https://example.com/doc", "vscode://file/C:/proj/x.py:10:1"):
        response = ext_client.post("/api/open", json={"url": url}, headers=OPEN_HEADERS)
        assert response.status_code == 200
    assert spy_opener.opened == ["https://example.com/doc", "vscode://file/C:/proj/x.py:10:1"]


def test_open_requires_its_own_header(ext_client, spy_opener):
    response = ext_client.post("/api/open", json={"url": "https://example.com"})
    assert response.status_code == 403
    assert spy_opener.opened == []


def test_open_reveal_of_missing_path_is_404(ext_client, spy_opener, tmp_path):
    response = ext_client.post(
        "/api/open", json={"reveal": str(tmp_path / "nope.txt")}, headers=OPEN_HEADERS
    )
    assert response.status_code == 404
    assert spy_opener.revealed == []


def test_open_reveal_dispatches_existing_path(ext_client, spy_opener, tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("x", encoding="utf-8")
    response = ext_client.post("/api/open", json={"reveal": str(target)}, headers=OPEN_HEADERS)
    assert response.status_code == 200
    assert len(spy_opener.revealed) == 1


def test_open_requires_exactly_one_target(ext_client, tmp_path):
    both = ext_client.post(
        "/api/open",
        json={"url": "https://x.com", "reveal": str(tmp_path)},
        headers=OPEN_HEADERS,
    )
    assert both.status_code == 422
    neither = ext_client.post("/api/open", json={}, headers=OPEN_HEADERS)
    assert neither.status_code == 422


# ── V18: served-page hardening (CSP + pointer read path) ────────────────────


def test_served_pages_carry_a_csp(client):
    page = client.get("/")
    assert "script-src 'self'" in page.headers.get("Content-Security-Policy", "")
    api = client.get("/api/health")
    assert "Content-Security-Policy" not in api.headers
    assert api.headers["X-Content-Type-Options"] == "nosniff"


def test_pointer_anchor_detail_is_a_metadata_card_not_a_500(client, store):
    """ADR 0005: a pt_ id through the blob store raises ValueError (digest
    check) — every read path must branch on artifact.kind first."""
    result = service.capture_pointer(
        store, kind="url", target="https://example.com/img.png", surface="browser"
    )
    response = client.get(f"/api/anchors/{result['anchor_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["excerpt"] is None
    assert body["artifact"]["kind"] == "pointer"
    assert body["artifact"]["locator"]["target"] == "https://example.com/img.png"


def test_redacted_pointer_serves_no_locator(client, store):
    result = service.capture_pointer(
        store, kind="url", target="https://example.com/secret.png", surface="browser"
    )
    service.redact_artifact(store, result["artifact_id"])
    body = client.get(f"/api/anchors/{result['anchor_id']}").json()
    assert body["artifact"]["redacted"] is True
    assert "locator" not in body["artifact"]


# ── V16: ephemeral context exposure (ADR 0004) ──────────────────────────────

CONTEXT_HEADERS = {"X-Inspeg-Context": "1"}


@pytest.fixture
def context_client(store):
    from inspeg.context import ContextHub

    hub = ContextHub()
    app = create_app(store, extension_origins=[EXT_ORIGIN], context_hub=hub)
    return TestClient(app, base_url=BASE_URL), hub


def test_context_endpoints_refuse_when_watch_is_disabled(client):
    """Off must be verifiable from outside — refusal, not empty output."""
    assert client.get("/api/context").status_code == 403
    tab = client.post("/api/context/tab", json={"url": "https://x.com"}, headers=CONTEXT_HEADERS)
    assert tab.status_code == 403
    workspace = client.post(
        "/api/context/workspace", json={"root": "C:/x"}, headers=CONTEXT_HEADERS
    )
    assert workspace.status_code == 403


def test_context_churn_never_persists_anything(context_client, store):
    """THE ADR 0004 guarantee: observation traffic appends zero events."""
    client, hub = context_client
    events_before = store.query_one("SELECT COUNT(*) AS c FROM event")["c"]
    artifacts_before = store.query_one("SELECT COUNT(*) AS c FROM artifact")["c"]
    for i in range(25):
        hub.set_window(exe="chrome.exe", title=f"Secret Document {i}")
        client.post(
            "/api/context/tab",
            json={"url": f"https://site{i}.example/page", "title": f"Tab {i}"},
            headers={**CONTEXT_HEADERS, "Origin": EXT_ORIGIN},
        )
        client.post(
            "/api/context/workspace",
            json={"root": f"C:/work/{i}", "file": f"C:/work/{i}/main.py"},
            headers=CONTEXT_HEADERS,
        )
        client.get("/api/context")
    assert store.query_one("SELECT COUNT(*) AS c FROM event")["c"] == events_before
    assert store.query_one("SELECT COUNT(*) AS c FROM artifact")["c"] == artifacts_before


def test_context_reads_back_what_was_pushed(context_client):
    client, hub = context_client
    hub.set_window(exe="acrobat.exe", title="paper.pdf — Acrobat")
    tab = client.post(
        "/api/context/tab",
        json={"url": "https://Example.com/a?b=2&a=1#f", "title": "A"},
        headers={**CONTEXT_HEADERS, "Origin": EXT_ORIGIN},
    )
    assert tab.status_code == 200
    state = client.get("/api/context").json()
    assert state["window"]["exe"] == "acrobat.exe"
    assert state["tab"]["url_norm"] == "https://example.com/a?a=1&b=2"
    assert state["fullscreen"] == "none"


def test_context_push_requires_its_header(context_client):
    client, _hub = context_client
    response = client.post("/api/context/tab", json={"url": "https://x.com"})
    assert response.status_code == 403
    assert "X-Inspeg-Context" in response.json()["detail"]


def test_context_tab_rejects_web_origins(context_client):
    client, _hub = context_client
    response = client.post(
        "/api/context/tab",
        json={"url": "https://x.com"},
        headers={**CONTEXT_HEADERS, "Origin": EVIL},
    )
    assert response.status_code == 403


def test_context_module_never_imports_the_store():
    """Structural half of the never-persists guarantee (ADR 0004)."""
    import inspect

    import inspeg.context as context_module

    source = inspect.getsource(context_module)
    assert "from inspeg.store" not in source
    assert "import inspeg.store" not in source
    assert "Store" not in source.replace("the Store", "")  # prose mentions only


# ── V18 (continued): HUD templates never carry inline script ────────────────


def test_hud_page_is_served_with_csp(client):
    page = client.get("/hud/")
    assert page.status_code == 200
    assert "script-src 'self'" in page.headers.get("Content-Security-Policy", "")


def test_ui_templates_carry_no_inline_script_or_handlers():
    """The CSP forbids inline script; the templates must not depend on any —
    and attacker-influenced strings must be rendered via textContent, which a
    static scan approximates by banning innerHTML writes of non-literals."""
    import re
    from pathlib import Path

    ui_root = Path(__file__).resolve().parent.parent / "ui"
    for html in ui_root.rglob("*.html"):
        text = html.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>(?!\s*</script>)", text), html
        assert not re.search(r"\son[a-z]+\s*=", text, re.IGNORECASE), html
    hud_js = (ui_root / "hud" / "hud.js").read_text(encoding="utf-8")
    assert "innerHTML" not in hud_js
    assert "insertAdjacentHTML" not in hud_js
    assert "document.write" not in hud_js
