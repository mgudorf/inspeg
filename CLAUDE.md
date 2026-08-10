# CLAUDE.md

inspeg — manual, multimodal capture into a provenance-anchored knowledge
graph. Local-first: one Python process owning SQLite + content-addressed
blobs, a FastAPI localhost API, a vanilla-JS quick-capture UI, and Chrome /
VS Code capture extensions (the old clipboard-capture hotkey is retired; the
one remaining hotkey toggles the HUD). Read `docs/architecture.md` before structural changes; it is
the plan of record.

## Commands

```
.venv\Scripts\activate                 # Windows venv
pip install -e ".[dev]"                # add ,hud for the pywebview HUD window
pre-commit install                     # gitleaks + ruff + invariant guards
pytest                                 # full suite, cross-platform
ruff check . && ruff format --check .  # CI runs both
python -m inspeg                       # daemon + HUD + context watch (127.0.0.1:8137)
python -m inspeg --no-hotkey --no-hud  # API/UI only, works on any platform
python scripts\build_vsix.py           # package the VS Code ext (no Node/vsce needed)
python -m inspeg --extension-origin chrome-extension://<id>   # allow the Chrome ext
python -m inspeg install|uninstall     # HKCU Run-key autostart (win32)
python -m inspeg reindex               # rebuild the FTS cache (daemon stopped)
```

## Hard invariants (architectural, not stylistic)

1. **Append-only event log.** `event` rows are never updated or deleted. The
   projection tables (`artifact`, `anchor`, `node`, `edge`, `support`) are
   written only through `Store.record` (event first, then apply) or rebuilt
   by `projection.replay`. Never INSERT/UPDATE them directly.
2. **No passive capture** (ADR 0004, three parts). **(2a)** Nothing is
   persisted without a deliberate user action — no code path may reach
   `Store.record` (hence log/projection/blobs/cache.db) as a consequence of
   observation alone. **(2b)** Nothing observes *content* passively: no
   clipboard reads, screen pixels, mic, keystroke hooks, or UIA scraping of
   other apps — ever. **(2c)** Sole exception: ephemeral display context —
   foreground-window *metadata* (exe, hwnd, title) and extension-pushed
   tab/workspace identity, held in daemon memory only, never persisted or
   transmitted, never used to trigger/enrich a capture, gated behind
   `--no-context-watch` which uninstalls the hooks entirely.
3. **Models propose; only humans assert.** Automated anything writes to
   `proposal`, never to `edge`. `event.actor` distinguishes
   `human` from `proposer:<name>` — that separation is the product.
4. **Types are nodes** (`props.kind = "edge_type"`), not strings.
   `edge.type` holds the type node's label purely as a denormalization.
   Predicates are a controlled ALL_CAPS vocabulary (ADR 0003): normalized to
   `^[A-Z][A-Z0-9_]*$`, never created implicitly by asserting an edge, and
   normalized mechanically in the projection so replay handles old events.
   Edges are removed/edited only via `edge_retracted` events (edits =
   retract + re-assert, evidence carried) — never by mutating rows.
5. **Blobs never go in the database** — content-addressed files under
   `blobs/<aa>/<sha256>`; `artifact.path` is data-dir-relative.
6. **Schema changes = new numbered file in `schema/`**, never an edit to an
   applied migration. Migrations are tracked by filename in
   `schema_migration`.
7. **The browser is the threat model.** The API is loopback-only and
   unauthenticated, so any page the user visits can reach it. Same-origin
   enforcement, the loopback `Host` allowlist, and the `X-Inspeg-Capture`
   header are load-bearing — never relax one without reading
   `docs/security.md`, and every control there has a test that fails if it is
   removed.
8. **Redaction and deletion are the only exceptions to blob immutability**
   (ADRs 0002, 0010), and both are *appended* events (`artifact_redacted`,
   `artifact_deleted` — an id-only tombstone), never a log rewrite. Delete
   removes the projection rows and the blob; redact keeps the provenance
   skeleton. Nodes and edges survive both.

## Layout

- `src/inspeg/store/` — `Store` façade (RLock write conn + read-only WAL
  conn, post-commit callbacks), blobstore, event log, projection + replay.
- `src/inspeg/service.py` — the only writers: `ingest_clipboard`,
  `ingest_web_capture`, `ingest_code_capture`, `capture_pointer`,
  `apply_label`/`remove_label`, `upgrade_artifact_source`, `assert_edge`,
  `redact_artifact`, `delete_artifact`, `create_predicate`.
- `src/inspeg/queries.py` — the read model (resolve/tree/labels/similar/
  queue), read-only connection only.
- `src/inspeg/context.py` — ephemeral ContextHub (ADR 0004); must never
  import the store (tested).
- `src/inspeg/fts.py` — FTS5 cache (`cache.db`), ephemeral, redaction-safe.
- `src/inspeg/hud.py` — pywebview HUD process (`inspeg-hud`); separate
  process because uvicorn owns the daemon's main thread (ADR 0009).
- `src/inspeg/adapters/` — `cfhtml.py` is pure/cross-platform;
  `clipboard.py`, `hotkey.py` (the ONE win32 message pump: all hotkeys +
  `SetWinEventHook` attachments), `foreground.py`, `openurl.py` are
  Windows-only with imports inside functions so the package imports
  everywhere.
- `src/inspeg/api/` — `app.py` factory (middleware: TrustedHost outermost,
  origin allowlist w/ extension carve-out, CSP) + routers: `capture.py`,
  `graph.py`, `query.py`, `context.py`, `dispatch.py` (`/api/open`),
  `stream.py` (SSE EventBus). `ui/` mounts at `/` last so `/api/*` wins.
- `extension/` — Chrome MV3 capture surface (pinned key; vendored anchoring
  under `extension/anchoring/` — never hand-write text anchoring).
- `vscode-ext/` — VS Code capture surface (plain JS, no build step).
- `ui/` — quick-capture app + `ui/hud/` (the HUD page; textContent-only
  rendering, enforced by a V18 test).
- `schema/`, `ui/` — repo-root dirs resolved by `util.resource_dir` (bundled
  into the wheel via hatch force-include).
- `tests/` — must pass on any OS; win32 code paths are never imported there.
  `test_security.py` holds one regression test per vector in
  `docs/security.md`; the shared `client` fixture uses a loopback base URL
  because the API rejects TestClient's default `testserver` Host.
- `.claude/` — repo agents (`security-auditor`, `replay-verifier`) and
  skills (`schema-migration`, `surface-smoke`, `release`);
  `scripts/check_invariants.py` backs the pre-commit invariant hook.

## Conventions

- Python 3.11+, Ruff for lint and format (line length 100).
- Provenance tiers: `exact` (1, M1 extension) / `sourced` (2, CF_HTML with
  SourceURL) / `attributed` (3, foreground app known) / `orphan` (4).
- IDs: artifacts use their sha256; anchors are deterministic
  (`anc_` + hash of artifact + selector) so re-captures dedupe; nodes/edges
  are `n_`/`e_` + uuid4 hex, minted in events and reproduced by replay.
- CF_HTML header offsets are **byte** offsets; selectors store **char**
  offsets into the decoded artifact text. `tests/helpers.py::build_cf_html`
  builds byte-accurate payloads.
- Dependencies must be permissive-licensed (verify at pin time). No Qt, no
  Electron, no Neo4j (GPL).

## Current milestone

M0 (vertical slice) is done: hotkey → CF_HTML parse → artifact + anchor →
typed edge → SQLite, quick-capture window only. Now building the
multi-surface plan (ADRs 0004–0009): Chrome + VS Code context-menu capture,
one-click labels, pointer artifacts, ephemeral context tracking, and a
docked graph HUD. The "use it fifty times" gate moves to the end of that
build (ADR 0004). Text anchoring is vendored, never hand-written: prefer
Hypothesis `match-quote` + `approx-string-match` (MIT), fallback
`dom-anchor-text-quote` (MIT) — verify licenses at pin time.
