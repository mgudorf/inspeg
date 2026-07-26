# CLAUDE.md

inspeg — manual, multimodal capture into a provenance-anchored knowledge
graph. Local-first: one Python process owning SQLite + content-addressed
blobs, a FastAPI localhost API, a vanilla-JS quick-capture UI, and a Windows
capture hotkey. Read `docs/architecture.md` before structural changes; it is
the plan of record.

## Commands

```
.venv\Scripts\activate                 # Windows venv
pip install -e ".[dev]"
pre-commit install                     # gitleaks secret scanning + ruff
pytest                                 # full suite, cross-platform
ruff check . && ruff format --check .  # CI runs both
python -m inspeg                       # run the daemon (UI at 127.0.0.1:8137)
python -m inspeg --no-hotkey           # API/UI only, works on any platform
```

## Hard invariants (architectural, not stylistic)

1. **Append-only event log.** `event` rows are never updated or deleted. The
   projection tables (`artifact`, `anchor`, `node`, `edge`, `support`) are
   written only through `Store.record` (event first, then apply) or rebuilt
   by `projection.replay`. Never INSERT/UPDATE them directly.
2. **No passive capture.** Nothing observes the clipboard, screen, or mic
   unless the user pressed something. Do not add listeners or polling.
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
8. **Redaction is the only exception to blob immutability** (ADR 0002), and
   it is an *appended* `artifact_redacted` event, never a log rewrite.

## Layout

- `src/inspeg/store/` — `Store` façade (one RLock, one connection), blobstore,
  event log, projection + replay.
- `src/inspeg/service.py` — the only writers: `ingest_clipboard`,
  `assert_edge`, `get_or_create_node`.
- `src/inspeg/adapters/` — `cfhtml.py` is pure/cross-platform; `clipboard.py`
  and `hotkey.py` are Windows-only with imports inside functions so the
  package imports everywhere.
- `src/inspeg/api/app.py` — FastAPI app factory; mounts `ui/` at `/` last so
  `/api/*` wins.
- `schema/`, `ui/` — repo-root dirs resolved by `util.resource_dir` (bundled
  into the wheel via hatch force-include).
- `tests/` — must pass on any OS; win32 code paths are never imported there.
  `test_security.py` holds one regression test per vector in
  `docs/security.md`; the shared `client` fixture uses a loopback base URL
  because the API rejects TestClient's default `testserver` Host.

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
typed edge → SQLite, quick-capture window only. Gate before M1: use it fifty
times. Next: M1 browser extension with tier-1 `TextQuoteSelector` anchoring
(vendor `dom-anchor-text-quote`; never hand-write text anchoring).
