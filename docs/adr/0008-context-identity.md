# ADR 0008 — Context identity: normalized source keys for "what do I have from here"

- Status: accepted
- Date: 2026-08-08

## Context

The HUD's central query is "what have I captured from *here*" — the current
URL, file, workspace, or application. Today `artifact.source_uri` stores the
raw URL (correctly — it is provenance, V4 keeps it display-only) and
`artifact.source_app` stores a composite `"chrome.exe | Page Title"` string.
Neither is queryable: the same page yields different raw URLs (fragments,
query order, trailing slashes) and the composite string mixes two facts.

## Decision

Pure, total helper functions in `src/inspeg/util.py` define context identity:

- `normalize_source_uri(uri)` — lowercase scheme and host, strip default
  port, drop fragment, sort query parameters, no trailing slash; returns
  `None` for anything unparseable. Never raises.
- `split_source_app(composite)` — best-effort split of the legacy
  `"exe | title"` composite on the first `" | "`; never raises.
- `derive_context_key(uri_norm, exe, locator)` — deterministic priority:
  `url:<uri_norm>` > `file:<canonical path>` > `workspace:<root>` >
  `app:<exe>` > `NULL`.

Storage:

- Event payloads carry structured `source_exe` / `source_title` going
  forward (alongside `source_app` for display compatibility).
- The projection (`artifact` v2) stores `source_uri_norm`, `source_exe`,
  `source_title`, `context_key`, each indexed as needed. For **legacy
  events** the applier derives them best-effort with the helpers above —
  the ADR 0003 two-layer pattern: the projection normalizes mechanically and
  never raises, so replay of old logs is deterministic and total.

## Consequences

- "What do I have from here" is one indexed query on `context_key` (exact)
  or `source_uri_norm` (URL), fast enough for a per-context-switch HUD
  refresh.
- The raw `source_uri` remains untouched provenance (V4: display-only, only
  `safe_url`-validated values become hrefs).
- Same log → same projection: the helpers are pure functions of the payload,
  so replay back-fills context columns for all history.
- URL normalization is deliberately conservative (no host aliasing, no
  tracking-parameter stripping); anything smarter is an M-later concern that
  must remain a pure function to keep replay deterministic.
