# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Graph page** (`/graph.html`): every edge in a table sortable by
  subject/predicate/object, with manual add, edit, and delete. Deleting
  records an `edge_retracted` event (reason `removed`); editing is
  retract-with-reason-`edited` + re-assert in one transaction, evidence
  anchors carried over — the log keeps the full correction history.
  New endpoints: `GET/PUT/DELETE /api/edges…`, `GET/POST /api/predicates`.
- Manual assertions without an evidence anchor are allowed and visibly
  weaker: the Evidence column shows 0.
- The Store now replays the event log automatically whenever a new schema
  migration lands, making "replay, don't migrate" the default behavior.

### Changed

- **Predicates are a controlled ALL_CAPS vocabulary**
  ([ADR 0003](docs/adr/0003-predicate-vocabulary.md)): labels normalize to
  `^[A-Z][A-Z0-9_]*$` (`instance of` → `INSTANCE_OF`), and asserting an edge
  no longer creates a predicate implicitly — creating one is a deliberate
  extra step (`POST /api/predicates` or `create_predicate: true`; one
  confirmation click in the UI). Migration `0003_predicate_vocabulary.sql`
  triggers a replay that re-projects pre-vocabulary data.
- **App-local HTML is no longer stored.** CF_HTML without a `SourceURL`
  (editors, IDEs, office apps) is styling markup with no provenance value;
  captures now keep only the plain text (from the text sibling, or the
  fragment stripped of tags). Browser captures with a `SourceURL` still store
  the full HTML with `href`s intact.

### Security

Full analysis of each vector, with the test that proves the fix, in
[docs/security.md](docs/security.md).

- **CSRF on the capture endpoint.** Any website could trigger a clipboard
  capture via a CORS simple request. Requests whose `Origin` does not match
  their `Host` are now rejected, and the capture endpoint requires a custom
  `X-Inspeg-Capture` header.
- **DNS rebinding.** The API accepted any `Host`, letting a remote site read
  and write the graph same-origin. `Host` is now restricted to loopback names.
- **Unauthenticated non-loopback bind.** `--host 0.0.0.0` silently exposed
  the API to the network; it is now refused without `--allow-remote`.
- **`javascript:`/`data:` URLs from forged clipboard payloads** could reach
  the UI's `href`. Source URLs are scheme-validated server-side and exposed
  as a separate `source_link` field.
- **Unbounded captures.** Ingest now rejects payloads over 16 MiB (HTTP 413),
  and excerpt generation parses at most 64 KiB of HTML instead of the whole
  document.
- **Path traversal hardening.** `BlobStore` validates that a digest is
  64 hex characters before using it as a path.
- **Concurrent daemons on one data dir** could corrupt the blobstore. An
  OS-level lock on `<data-dir>/.lock` enforces one process per data dir, and
  blob temp files are unique per writer.
- **Clipboard could be left locked system-wide** if an interrupt landed
  between `OpenClipboard` and its `finally`.

### Added

- **Redaction** ([ADR 0002](docs/adr/0002-redaction.md)): captured content can
  be destroyed while keeping its provenance skeleton, via
  `POST /api/artifacts/{id}/redact`. Records an `artifact_redacted` event, so
  the log stays append-only and `replay` does not resurrect the content.
  Schema migration `0002_artifact_redaction.sql`.
- `tests/test_security.py` — a regression test per patched vector, each
  verified by disabling its control and confirming the test fails.
- Secret-scanning pipeline: gitleaks in pre-commit and a CI job scanning the
  full git history, plus `detect-private-key` and large-file checks.
- `/api/health` reports hotkey status (`registered`/`failed`/`stopped`).
- Startup sweep removes crash leftovers (`.tmp` files, blobs from rolled-back
  captures).

### Changed

- **Default hotkey is now `ctrl+shift+alt+i`** (was `ctrl+alt+a`, briefly
  `win+shift+a`): Windows synthesizes AltGr as Ctrl+Alt, so the original
  default fired while users on non-US keyboard layouts were typing, and
  `win+shift+a` turned out to collide with existing app registrations.
  Requiring Shift on top of Ctrl+Alt keeps plain AltGr typing safe.
- Captures run on a worker thread instead of the hotkey message loop, so a
  slow capture no longer freezes the hotkey.
- Closing the console window now releases the store lock and unregisters the
  hotkey via `SetConsoleCtrlHandler`; all background threads are daemons.
- A missing blob returns 410 Gone instead of a 500.

### Fixed

- The hotkey no longer dies silently: a failed registration and a
  `GetMessageW` error are both logged and reflected in `/api/health`.

## [0.1.0] - 2026-07-26

### Added — M0 vertical slice

- Content-addressed blob store (`blobs/<aa>/<sha256>`) with automatic dedup.
- Append-only event log in SQLite; artifact/anchor/node/edge/support tables
  are a projection rebuilt by `replay`.
- CF_HTML clipboard parser: `SourceURL` detection for provenance tier 2,
  byte-to-char offset mapping for fragment anchors.
- Global capture hotkey (Ctrl+Alt+A, Windows) via `RegisterHotKey`; captures
  HTML and plain-text clipboard formats as sibling artifacts.
- Provenance tiers `sourced`/`attributed`/`orphan` assigned at capture time
  (tier 1 `exact` arrives with the browser extension in M1).
- FastAPI daemon on 127.0.0.1 serving the JSON API and the quick-capture UI.
- Quick-capture window: shows the captured span, its tier and source; asserts
  subject —predicate→ object triples with the anchor attached as evidence.
- Edge types are nodes (`props.kind = "edge_type"`), denormalized into
  `edge.type` for query speed.
- Schema migrations in `schema/`, applied by filename, tracked in
  `schema_migration`.
