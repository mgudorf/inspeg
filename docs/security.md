# Patched attack vectors

A record of concrete attacks against inspeg, what stopped each one, and the
test that fails if the control is removed. `SECURITY.md` states the threat
model and what is still *not* defended; this file is the engineering log.

Every control below was verified by mutation: the control was disabled, the
named test was confirmed to fail, and the control was restored. A control
without a failing test is not a control.

All tests live in [`tests/test_security.py`](../tests/test_security.py).

## The core problem: a localhost API is reachable from the web

The daemon binds `127.0.0.1` with no authentication, which is the standard
posture for a local tool — but "localhost" is not a security boundary against
a *browser*. Any page the user visits can send requests to `127.0.0.1:8137`,
and the browser will attach no credentials but will happily deliver the
request. V1 and V2 are the two ways that becomes an exploit; they are the most
serious issues found and the reason the middleware exists.

---

## V1 — Cross-site request forgery on the capture endpoint

**Severity: high.** `POST /api/captures/clipboard` took no body and no
special headers, which made it a CORS *simple request*: any page could issue
`fetch("http://127.0.0.1:8137/api/captures/clipboard", {method: "POST", mode: "no-cors"})`
and the browser would send it. The browser blocks the attacker from reading
the *response*, but the request already ran — the daemon read the clipboard
and wrote it to permanent storage. On a timer, a malicious page harvests
whatever passes through the clipboard, including a password just copied out
of a password manager. Chained with V2, the attacker reads it back out.

This also silently broke invariant #2 (no passive capture): captures happened
without the user pressing anything.

**Fixed by** two independent controls in
[`api/app.py`](../src/inspeg/api/app.py):

1. A `reject_cross_origin` middleware. Browsers set `Origin` on every POST
   (and on any cross-origin request); if `Origin` is present and does not
   match the request's own `Host`, the request is rejected with 403. Requests
   with no `Origin` — curl, the adapters, the local UI's same-origin GETs —
   are unaffected, so non-browser clients keep working.
2. The capture endpoint additionally requires an `X-Inspeg-Capture` header.
   A custom header forces a CORS preflight, and the preflight fails because
   nothing sends the permissive response headers a browser would need. This
   holds even if the Origin check is ever bypassed.

**Tests:** `test_cross_origin_capture_is_rejected`,
`test_cross_origin_edge_assertion_is_rejected`, `test_cross_origin_read_is_rejected`,
`test_capture_without_csrf_header_is_rejected`, plus
`test_same_origin_requests_still_work` and
`test_non_browser_clients_without_origin_still_work` so the fix cannot be
"secure because nothing works".

## V2 — DNS rebinding gave remote sites full read/write access

**Severity: high.** The app never validated the `Host` header. An attacker
serves a page from `evil.example`, then rebinds that name's DNS to
`127.0.0.1`. The browser now treats `http://evil.example:8137` as *same
origin* as the attacker's page, so the Origin check in V1 no longer helps:
the attacker reads every endpoint — the whole graph, every excerpt — and
writes assertions. Combined with V1, freshly captured clipboard content is
exfiltrated.

**Fixed by** `TrustedHostMiddleware` with an allowlist of `127.0.0.1` and
`localhost`. A rebound request carries `Host: evil.example` and is rejected
with 400 before any handler runs. `--allow-remote` widens the allowlist for
users who deliberately expose the daemon (see V3).

**Tests:** `test_rebound_host_header_is_rejected`, `test_loopback_hosts_are_allowed`,
`test_allow_remote_opt_in_disables_the_host_allowlist`. The shared test client in
`conftest.py` uses a loopback base URL specifically because the default
(`testserver`) is now correctly rejected.

## V3 — `--host` accepted any interface with no authentication

**Severity: high.** `--host` help text said "keep it local" but nothing
enforced it. `--host 0.0.0.0` handed every device on the network full
unauthenticated read/write access to the capture store, plus the ability to
trigger clipboard reads.

**Fixed by** a loopback check in [`__main__.py`](../src/inspeg/__main__.py):
a non-loopback `--host` is refused (argparse exit code 2, before anything
binds) unless the user passes `--allow-remote`, whose help text spells out
the consequence. Opting in also logs a warning at startup and widens the V2
allowlist, so the two settings cannot drift out of agreement.

**Tests:** `test_non_loopback_bind_is_refused_without_opt_in`, `test_loopback_detection`.

## V4 — Hostile URL schemes from forged clipboard data

**Severity: medium.** `SourceURL` in a CF_HTML payload is attacker-controllable:
*any* local application can put a crafted payload on the clipboard, and the
parser stored it verbatim. The UI then assigned it straight to `link.href`,
so a capture could yield a `javascript:` or `data:` link in the
quick-capture window.

**Fixed by** `service.safe_url`, which returns the URL only when its scheme is
`http` or `https`. The API exposes the validated value as a separate
`source_link` field and the UI renders *only* that as an `href`. The raw
`source_uri` is still returned and stored — it is provenance, and discarding
it would lose evidence — but it is display-only text.

**Tests:** `test_dangerous_url_schemes_are_never_linkable` (parametrized over
`javascript:`, mixed-case `JavaScript:`, `data:`, `vbscript:`, `file:`),
`test_http_urls_are_linkable`, `test_forged_source_url_is_not_served_as_a_link`.

## V5 — Unbounded captures exhausted memory and disk

**Severity: medium.** A capture was read, decoded, stored (twice when HTML and
text siblings both exist), and then passed *in full* to BeautifulSoup just to
produce a 500-character excerpt. Copying a very large document spiked memory
to several times the payload size and blocked the hotkey thread; the same
full-document parse ran again on every anchor view.

**Fixed by** three bounds:

- `MAX_CAPTURE_BYTES` (16 MiB) rejects oversized snapshots at ingest with
  `CaptureTooLargeError` → HTTP 413. Nothing is written: no blob, no event.
- `EXCERPT_HTML_SLICE` (64 KiB) bounds what reaches the HTML parser, at both
  ingest and read time. The excerpt is 500 characters; parsing megabytes to
  produce it was pure waste.
- `/api/nodes` already clamped `limit` to 100; there is now a test so it stays
  clamped.

**Tests:** `test_oversized_capture_is_refused`,
`test_excerpt_generation_does_not_parse_the_whole_document`,
`test_node_search_limit_is_clamped`.

## V6 — Digest values were used as file paths unvalidated

**Severity: low today, high the moment an artifact endpoint is added.**
`BlobStore.relpath` interpolated its `digest` argument into a filesystem path
with no validation. Callers only ever passed real sha256 digests, so this was
not exploitable — but it is one route handler away from a path traversal
primitive (`GET /api/artifacts/../../../../etc/passwd`).

**Fixed by** validating in `relpath` against `[0-9a-f]{64}`, which every read
and write path already funnels through, so the check cannot be bypassed by
adding a caller. Rejecting non-canonical forms (uppercase hex) keeps one blob
to one path.

**Tests:** `test_blobstore_rejects_non_digest_paths` (parametrized over
traversal strings, encoded traversal, empty, wrong length, non-hex, uppercase),
`test_blobstore_accepts_real_digests`.

## V7 — SQL injection and stored XSS (pre-existing defences, now pinned)

**Severity: none found.** Every query was already parameterized, `LIKE`
patterns already escaped `\`, `%`, `_`, and the UI already escaped HTML on
render. Nothing to fix — but nothing tested it either, so the next refactor
could quietly remove it.

**Tests:** `test_sql_injection_in_labels_is_inert` (a `DROP TABLE` payload
round-trips as data and the table survives), `test_sql_wildcards_in_search_are_literal`
(`_` and `%` match themselves, not "any character"),
`test_script_payload_in_a_label_survives_as_inert_data`.

## V8 — Two daemons could corrupt one data directory

**Severity: medium.** `Store`'s `RLock` serializes *threads*, not processes.
Two daemons on different ports sharing a `--data-dir` raced the projection and
the blobstore — and the blobstore made that worse by using a fixed temp name
(`<digest>.tmp`), so two writers of the same content could clobber each
other's half-written file and `os.replace` a truncated blob into place.

**Fixed by** an OS-level advisory lock (`msvcrt.locking` on Windows,
`fcntl.flock` elsewhere) on `<data-dir>/.lock`, taken in `Store.__init__` and
released in `close()`. A second daemon exits with a clear message instead of
corrupting data. Because the OS drops the lock when the process dies, there
are no stale locks to reap after a crash. Temp files are now
`<digest>.<pid>.<uuid4>.tmp`, unique per writer.

**Tests:** `test_second_store_on_the_same_data_dir_is_refused`,
`test_lock_is_released_on_close`, `test_close_is_idempotent`,
`test_concurrent_puts_of_same_content_use_distinct_temp_files`.

## V9 — Accidentally captured secrets could never be deleted

**Severity: medium.** The append-only log plus content-addressed blobs meant
there was *no* path to destroy a capture. Copy a password, press the hotkey,
and it is on disk forever: deleting the projection row does nothing, because
`replay` rebuilds it from the log. For a tool whose entire job is capturing
whatever is on the clipboard, "no undo" is a real hazard, not a purity win.

**Fixed by** a redaction path — the one sanctioned exception to blob
immutability, specified in [ADR 0002](adr/0002-redaction.md).
`service.redact_artifact` records an `artifact_redacted` event, sets
`artifact.redacted`, and deletes the blob file. The log stays append-only
(redaction is an *appended* event, not a deletion), replay reproduces the
flag, and the provenance skeleton — that something was captured, when, from
where — survives. `POST /api/artifacts/{id}/redact` exposes it.

**Tests:** `test_redaction_deletes_the_blob_and_flags_the_artifact`,
`test_redaction_survives_replay` (the important one: the log must not
resurrect content), `test_redacted_artifact_serves_no_excerpt`,
`test_redaction_is_idempotent_and_appends_one_event`,
`test_redacting_unknown_artifact_raises`,
`test_redact_endpoint_404s_for_unknown_artifact`.

**Extended by hard delete (ADR 0010):** `artifact_deleted` removes the
projection rows *and* the blob, leaving only an id tombstone in the log —
for captures that should not exist at all (vs. redaction's "destroy content,
keep the record"). The FTS cache row is removed synchronously in the same
commit callback, and the startup reconcile purges any indexed id that is no
longer a live unredacted blob (crash backstop). `DELETE /api/artifacts/{id}`
requires same-origin + the capture header and is **not** on the extension
route allowlist — destructive curation is a human-at-the-HUD action.
Tests: `test_delete_removes_everything_but_the_node`,
`test_delete_replays_deterministically`,
`test_delete_reaches_fts_synchronously`,
`test_fts_reconcile_purges_deleted_artifacts_on_reopen`,
`test_delete_endpoint_requires_capture_header`,
`test_extension_origin_cannot_delete_artifacts`.

## V10 — Missing blob returned a 500

**Severity: low.** If a blob file vanished (manual deletion, failed sync,
redaction), `store.blobs.get` raised `FileNotFoundError` and the anchor
endpoint returned an opaque 500 with a stack trace.

**Fixed by** catching it and returning 410 Gone with the artifact id.
Redacted artifacts are handled before the read, so they return 200 with a
null excerpt rather than an error — a redaction is an intended state, not a
fault.

**Tests:** `test_missing_blob_returns_410_not_500`.

## V11 — Crash leftovers accumulated forever

**Severity: low.** A crash between `tmp.write_bytes` and `os.replace` left a
`.tmp` file behind permanently. A rolled-back capture transaction left a blob
with no artifact row, because the blob is written before the event commits.
Neither is a correctness bug; both accrete disk indefinitely.

**Fixed by** `BlobStore.sweep`, called once at startup under the data-dir
lock: it deletes `.tmp` files always, and blobs with no non-redacted artifact
row when the event log is non-empty. The empty-log guard matters — a fresh
database beside existing blobs means the database is not the one that
produced them, and sweeping there would be data loss rather than cleanup.

**Tests:** `test_startup_sweep_removes_tmp_files_but_keeps_live_blobs`,
`test_sweep_does_not_touch_blobs_when_the_log_is_empty`.

## V12 — The default hotkey collided with AltGr

**Severity: low, but user-hostile.** The default was `ctrl+alt+a`. Windows
synthesizes AltGr as Ctrl+Alt, so on German, Polish, Spanish and many other
layouts, a user typing an ordinary AltGr character would fire an unintended
capture — passive capture by accident, on a machine the developer never tests
on. Microsoft's own guidance is to avoid Ctrl+Alt for hotkeys.

**Fixed by** requiring more than Ctrl+Alt in the default. The default was
first moved to `win+shift+a`, which proved to collide with other apps'
registrations in practice, then to `ctrl+shift+alt+i`. The clipboard-capture
hotkey has since been retired entirely (capture lives in the Chrome and
VS Code context menus); the property now guards the one remaining default,
the HUD toggle `ctrl+shift+alt+g`. That still contains Ctrl+Alt, but plain
AltGr typing never sets Shift, so AltGr characters cannot fire it.
`--hud-hotkey` still accepts anything, including bare Ctrl+Alt for users who
want it.

Note that `RegisterHotKey` cannot distinguish left from right modifiers
(left-only would require a low-level keyboard hook — passive observation of
every keystroke, which this project forbids), so right-side modifiers fire
the hotkey too.

**Tests:** `test_default_hotkey_avoids_bare_ctrl_alt` (asserts the property,
not the literal string, so it keeps holding if the default changes again),
`test_hotkey_requires_a_modifier`.

## V13 — Clipboard could be left locked system-wide

**Severity: low.** `OpenClipboard()` was called *outside* the `try` whose
`finally` calls `CloseClipboard()`. An interrupt landing in that window left
the Windows clipboard held open by inspeg — every other application on the
machine locked out of copy and paste until the process exited.

**Fixed by** moving the open inside the retry loop and putting all clipboard
access under the `finally` that releases it. The retry loop now also
preserves the last error for the `ClipboardBusyError` cause chain rather than
discarding it.

## V14 — Background work outliving the terminal

**Severity: low.** Closing the console window delivers `CTRL_CLOSE_EVENT` and
then terminates the process; Python's `finally` and `atexit` may never run,
so the store's file lock and SQLite connection were released only by OS
teardown.

**Fixed by** three things that make "close the window" a clean stop:

- Every background thread is a daemon thread — the hotkey listener
  (`HotkeyListener`) and the per-capture worker. Daemon threads cannot keep
  the process alive, so nothing survives the window closing.
- `SetConsoleCtrlHandler` runs the same `cleanup()` as the normal shutdown
  path on `CTRL_CLOSE_EVENT`, `CTRL_LOGOFF_EVENT`, and `CTRL_SHUTDOWN_EVENT`:
  it unregisters the hotkey and closes the store, releasing the data-dir lock.
  Ctrl+C and Ctrl+Break are left alone so uvicorn's own shutdown still runs.
- `Store.close()` is idempotent, since the console handler and the `finally`
  block can both fire.

**Tests:** `test_hotkey_listener_is_a_daemon_thread`, `test_close_is_idempotent`.

## Also fixed, without a dedicated attack

- **Capture moved off the message-loop thread.** The hotkey callback ran
  ingestion and `webbrowser.open` on the thread pumping Windows messages, so
  a slow capture froze the hotkey. It now hands off to a daemon worker.
- **The hotkey no longer fails silently.** A failed `RegisterHotKey` only
  logged, and `GetMessageW` returning `-1` ended the loop with no message, so
  the daemon could run for hours with a dead hotkey. The listener now tracks
  `status` (`pending`/`registered`/`failed`/`stopped`), logs the `-1` case,
  and `/api/health` reports it.
- **Workflow token scope.** `.github/workflows/ci.yml` declares
  `permissions: contents: read`.

## V15 — Extension-origin allowlist

**Severity: high if done wrong — this is a deliberate carve-out of V1.**
Chrome MV3 service workers send `Origin: chrome-extension://<id>` on every
POST, so the V1 middleware 403'd the entire browser-capture surface.
Relaxing the origin check naively (a wildcard, a runtime-mutable list, or a
prefix match) would reopen the exact CSRF class V1 closed.

**Fixed by** an exact-match allowlist (ADR 0007), restructured — not
weakened — in `api/app.py`:

1. Origins come only from `--extension-origin` / `create_app`; each must
   match `chrome-extension://<32 chars of a-p>` exactly or startup refuses.
   **No API can mutate the allowlist.**
2. An allowlisted origin is accepted only on the extension-permitted routes
   (`_EXTENSION_PATHS`, least privilege) — everywhere else it is treated
   like any cross origin. `/api/open` and `/api/edges` are out of reach.
3. `X-Inspeg-Capture` stays mandatory on every write route.
4. The OPTIONS preflight echoes exactly the one matched origin — never `*` —
   and only the headers the extension needs.
5. TrustedHost (V2) stays outermost and untouched.

**Tests:** `test_allowlisted_extension_origin_is_accepted`,
`test_non_allowlisted_extension_origin_is_rejected`,
`test_web_origins_still_rejected_when_allowlist_configured`,
`test_extension_origin_still_requires_capture_header`,
`test_extension_origin_is_confined_to_extension_routes`,
`test_malformed_or_wildcard_extension_origin_is_refused`,
`test_preflight_echoes_exactly_the_matched_origin`,
`test_preflight_for_web_origin_still_fails` — plus V1–V14 untouched and
green.

## V17 — Deep-link dispatch from stored data

**Severity: high the moment the HUD exists.** `POST /api/open` launches
external targets that originate in *stored, attacker-influenceable data*
(captured URLs, file paths) — V6's lesson applied to launching. Unfiltered,
this is `os.startfile("ms-settings:...")` or a UNC path away from arbitrary
protocol-handler invocation.

**Fixed by** three controls in `api/dispatch.py`:

1. Scheme allowlist `{http, https, vscode, vscode-insiders}`; everything
   else — `javascript:`, `file:`, `data:`, `ms-settings:`, UNC — is 422 and
   never reaches the dispatcher.
2. `reveal` uses `SHOpenFolderAndSelectItems` (never `explorer.exe /select`
   string interpolation), only for paths that exist.
3. Same-origin only (extension origins are excluded by V15 path scoping and
   rechecked in-route) plus a mandatory `X-Inspeg-Open` header — the V1
   preflight-forcing pattern.

**Tests:** `test_open_refuses_non_allowlisted_schemes` (parametrized),
`test_open_allows_https_and_vscode_links`, `test_open_requires_its_own_header`,
`test_open_reveal_of_missing_path_is_404`,
`test_open_reveal_dispatches_existing_path`,
`test_open_requires_exactly_one_target`, and the extension-origin exclusion
in `test_extension_origin_is_confined_to_extension_routes`. Tests inject a
spy dispatcher — nothing launches during the suite.

## V18 — UI injection via stored and context-derived strings

**Severity: medium.** The served UIs render three attacker-influenced string
classes: stored data (V7-tested), window/tab titles pushed into the context
layer (*not* covered by V7 — any app titles its own windows), and label
text. The HUD renders inside WebView2 same-origin with the whole API, so a
script foothold there is a full read/write compromise.

**Fixed so far by:** a CSP on every served page (`script-src 'self'`, no
inline, `frame-ancestors 'none'`) plus `X-Content-Type-Options: nosniff` in
`api/app.py`; pointer artifacts branch before any blob access so a `pt_` id
cannot 500 the read path; redacted pointers serve no locator. Still owed by
the HUD phase: text-node-only rendering asserted over HUD templates and
ContextHub fields, and pywebview navigation interception (external links
open in the system browser — a hostile page must never load inside the HUD
window).

**Tests:** `test_served_pages_carry_a_csp`,
`test_pointer_anchor_detail_is_a_metadata_card_not_a_500`,
`test_redacted_pointer_serves_no_locator`.

## V16 — Ephemeral context exposure

**Severity: medium, accepted deliberately (ADR 0004).** The context layer
gives the HUD "what am I looking at right now" — foreground exe/title plus
extension-pushed tab/workspace. Two risks: (a) any local process can read
live activity via `GET /api/context` — a strict escalation of the accepted
"any local process can read your captures" posture; (b) a bug could leak
observation into the permanent store, silently breaking invariant 2a.

**Controlled by:**

1. **Never persisted, structurally.** `inspeg/context.py` does not import
   the Store (asserted by test); the win32 watcher only ever calls
   `ContextHub` setters; state is process-memory with TTLs
   (`context.py`). The regression test drives 25 rounds of window/tab/
   workspace churn and asserts the event and artifact censuses are
   byte-for-byte unchanged.
2. **Off means off.** `--no-context-watch` never installs the
   `SetWinEventHook`s (the `on_pump_start` attachment is simply absent) and
   every `/api/context*` endpoint refuses with 403 — "disabled" is
   verifiable from outside, not indistinguishable from "idle".
3. **Push endpoints are guarded** like every write: `X-Inspeg-Context`
   header (preflight-forcing, the V1 pattern); `/api/context/tab` accepts
   only the allowlisted extension origin (V15 path scoping); hostile web
   origins get 403.
4. Risk (a) is documented in `SECURITY.md` — window titles are
   quasi-content (document names, mail subjects); the toggle bounds this,
   it does not eliminate it.

**Tests:** `test_context_endpoints_refuse_when_watch_is_disabled`,
`test_context_churn_never_persists_anything`,
`test_context_reads_back_what_was_pushed`,
`test_context_push_requires_its_header`,
`test_context_tab_rejects_web_origins`,
`test_context_module_never_imports_the_store`.

## Not fixed — accepted risk

These are real, known, and deliberately out of scope. They belong to the
threat model in `SECURITY.md`, not to this log.

- **No authentication.** Any *process* on the machine can still reach the API.
  The controls above defend against remote web pages, not against local code
  running as the user — which could read `~/.inspeg` directly anyway. Note
  the V16 escalation: while the context watcher is on, that includes *live
  activity*, not just stored captures.
- **The pinned Chrome extension is in the trusted computing base.** An
  unpacked extension directory is writable by any same-user process, and a
  compromised extension holds a valid allowlisted origin with full
  capture-write and context-push capability. Native messaging (a stdio host
  with OS-attested caller identity, the KeePassXC-proxy pattern) would
  retire V1/V2 for the browser channel entirely and is the designated
  hardening step if this ever matters in practice.
- **One-click browser capture stores the full page text** as the implicit
  Document artifact — a one-word selection on a webmail page stores the
  page. This is what makes provenance-quality anchoring possible; the
  mitigations are disclosure (this line) and one-click redaction, not
  prevention.
- **URL digests are an oracle for guessable URLs.** `/api/anchors/url-digests`
  hands the extension SHA-256s of captured page URLs; anything that can read
  extension storage can test "did they capture this page" for URLs it can
  guess. Accepted: the alternative (per-page resolve calls) ships actual
  browsing history.
- **Window titles are quasi-content.** ADR 0004 permits observing them for
  the HUD; titles leak document names and mail subjects. `--no-context-watch`
  bounds this; it does not eliminate it while on.
- **No encryption at rest.** Blobs and the database are plain files. Use
  full-disk encryption.
- **Captured HTML is stored verbatim.** The UI renders extracted text, never
  the HTML, but exports built later must do their own sanitization.
- **CI actions are pinned by tag, not SHA.** Tags are mutable; SHA pinning is
  the hardened posture. Deferred as disproportionate at this scale.
- **SQLite WAL on a syncing filesystem** (OneDrive, Dropbox) can corrupt the
  database. Do not point `--data-dir` at one.
