# ADR 0002 — Redaction: destroying captured content without breaking the log

- Status: accepted
- Date: 2026-07-26

## Context

[ADR 0001](0001-append-only-log.md) makes the event log immutable and the
graph a projection of it. Combined with content-addressed blobs, that gave
captured content a property nobody chose: it could never be destroyed.

That is a hazard, not a purity win. inspeg captures whatever is on the
clipboard at the moment the hotkey fires. Sooner or later that is a password
copied out of a password manager, a private key, a message that was never
meant to be kept. Before this ADR there was no recovery: deleting the blob
left a dangling `artifact.path` and a 500 on read, and deleting the
projection row did nothing at all, because `replay` rebuilds it from the log.
The only remedy was to destroy the entire data directory.

"Be careful what you copy" is not a control. A tool that captures on a
keystroke needs an undo.

## Decision

Redaction is the single sanctioned exception to blob immutability, and it is
expressed as an *append*, not a deletion.

`service.redact_artifact(store, artifact_id)`:

1. appends an `artifact_redacted` event (actor recorded as usual);
2. sets `artifact.redacted = 1` via the projection applier;
3. deletes the blob file from disk.

What is destroyed is the **content**. What survives is the **provenance
skeleton**: that an artifact existed, its hash, when it was captured, from
what URL and application, and every anchor and edge that cited it. An
assertion supported by redacted evidence stays visible and stays attributed —
it does not silently become unsupported.

The log itself is never rewritten. `artifact_redacted` is an ordinary event
in sequence, so `replay` reproduces the redaction exactly like any other
state, and the history of *when the user redacted* is preserved — which is
itself decision-record data of the kind ADR 0001 exists to keep.

## Alternatives rejected

- **Rewrite or delete the original `artifact_added` event.** Breaks the
  append-only invariant that everything else depends on, and destroys the
  evidence that a capture ever happened — which is exactly what the user
  needs to audit after an accidental capture.
- **Flag it and keep the blob.** Does not solve the problem. The secret is
  still on disk in the clear.
- **Delete the artifact row entirely.** Anchors and edges reference it;
  cascading the delete would silently destroy human assertions because of a
  problem with their evidence.

## Consequences

- `artifact.redacted` is added by `schema/0002_artifact_redaction.sql`. The
  API returns it, serves a null excerpt for redacted artifacts, and the UI
  shows `(redacted)`.
- Content-addressing means redaction is per *content*, not per capture: two
  captures of identical bytes share one blob and one artifact row, so
  redacting affects both. This is correct — the bytes are the secret — but it
  is worth knowing.
- `redact_artifact` is idempotent: re-redacting appends no second event and
  the blob unlink tolerates a missing file.
- The startup sweep (`BlobStore.sweep`) treats redacted artifacts as
  not-to-be-kept, so a blob that survived a crash mid-redaction is collected
  on the next start.
- Redaction is currently reachable via `POST /api/artifacts/{id}/redact`. The
  quick-capture UI does not yet expose a button; that is a UI gap, not a
  model gap.
- Exports (M5) must honour `redacted` — a redacted artifact must never appear
  in a training dump with content. That constraint lands with the exporter.
