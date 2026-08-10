# ADR 0010 — Hard delete

**Status:** accepted (2026-08-09)

## Context

Redaction (ADR 0002) destroys an artifact's *content* but deliberately keeps
its provenance skeleton — the row, its anchors, its labels — so the graph
remembers that something existed. Real use immediately produced the other
need: captures that should simply not exist (test noise, misfires, things
captured twice by accident). Forcing those through redaction leaves ghost
rows in every view, which reads as clutter, not provenance.

## Decision

A second sanctioned destruction path: **`artifact_deleted`**, an appended
event whose payload is the artifact id and nothing else (an id-only
tombstone). It removes, via the projection applier:

- the `artifact` row,
- its anchors, and every `support` row hanging off them (labels and edge
  evidence alike),
- its `capture_member` rows,
- `derived_from` references pointing at it (nulled, not cascaded).

The **blob file is unlinked by the service** after the event commits — same
path-shape guard as redaction — never by the applier, so replay stays a pure
DB operation. The FTS cache row is removed synchronously in the post-commit
callback, with the startup reconcile as the crash backstop (keep-set: any
indexed id that is not a live unredacted blob is purged).

**Nodes and edges survive.** They are asserted knowledge, not capture rows;
deleting the evidence weakens an edge (its evidence count drops), it does not
retract it. Retraction stays its own deliberate act (`edge_retracted`).

The event log keeps the full history including the tombstone — invariant #1
(append-only log) is untouched; replay reproduces the deletion
deterministically. Invariant #8 is amended: redaction **and deletion** are
the two exceptions to blob immutability, both event-sourced.

## Consequences

- Delete is strictly stronger than redact and the HUD offers both: *redact*
  = "destroy content, keep the where/when record", *delete* = "this never
  belonged here".
- `DELETE /api/artifacts/{id}` requires same-origin + the capture header and
  is deliberately **not** on the extension route allowlist (V15): destructive
  curation is a human-at-the-HUD action.
- A deleted artifact's id can reappear if the same content is captured again
  (ids are content hashes); the new capture is a new event after the
  tombstone, so both live and replayed projections converge on "present".
- The log grows monotonically even as the projection shrinks — acceptable;
  log compaction remains a non-goal (ADR 0001).
