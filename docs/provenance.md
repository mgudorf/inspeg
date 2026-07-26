# Provenance tiers

Every artifact records how well it can be traced back to its source. The tier
is stored as a column (`artifact.provenance`) and is filterable — recording
the weakness beats discovering it during training.

| Tier | Name | What you have | Source |
|---|---|---|---|
| 1 | `exact` | URL + char offsets + content hash | browser extension (M1) |
| 2 | `sourced` | `SourceURL` from CF_HTML + fragment HTML | clipboard from a browser |
| 3 | `attributed` | app name + window title + timestamp | clipboard from anywhere else |
| 4 | `orphan` | blob + timestamp only | screenshot, mic, file drop, bare text |

Tier 4 is fine as commentary and useless as evidence.

## How M0 assigns tiers

The clipboard adapter reads the `HTML Format` (CF_HTML) payload when present.
Its description header is parsed by `inspeg.adapters.cfhtml`:

- **`SourceURL` present** → tier 2 (`sourced`). The fragment HTML is kept
  with `href`s intact, and the anchor selects exactly the
  `StartFragment`/`EndFragment` span inside the stored HTML artifact.
- **No `SourceURL`** (copied from Word, an IDE, etc.) → tier 3
  (`attributed`): the foreground process image name and window title at
  capture time are recorded in `artifact.source_app`.
- **Plain text only, foreground app known** → tier 3.
- **Plain text only, nothing else known** → tier 4 (`orphan`).

Tier 1 requires real character offsets into a document the user explicitly
established — that is what the M1 browser extension's document-then-span flow
buys, and it cannot be retrofitted onto clipboard data.

## Practical notes

- CF_HTML header offsets are byte offsets into the UTF-8 payload; anchors
  store character offsets into the decoded artifact text. The conversion
  happens once, at parse time.
- One copy often yields HTML and plain text simultaneously. Both are stored
  as sibling artifacts (same `capture_id` in the event log, same tier); the
  anchor points at the HTML artifact, which is the richer evidence.
- The content hash (= artifact id) is the rot detector: when a source page
  changes, re-located quotes can be checked against the stored document
  rather than trusted blindly.
