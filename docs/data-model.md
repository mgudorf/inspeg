# Data model (as implemented, M0)

The full rationale is in [architecture.md](architecture.md) §6. This page
documents what exists in code today.

## Database of record: the event log

```sql
event(seq, ts, kind, payload JSON, actor)
```

Everything below it is a projection. `Store.record(kind, payload, actor)`
appends the event and applies it to the projection in the same transaction;
`Store.replay()` clears the projection tables and re-applies every event in
order. Event kinds the projection doesn't recognize are skipped — the log may
carry richer history than any projection consumes.

`actor` is `human` or `proposer:<name>`. It is what separates your judgments
from a model's suggestions.

### Event kinds (M0)

| kind | payload | projected into |
|---|---|---|
| `artifact_added` | artifact columns + `capture_id` | `artifact` (INSERT OR IGNORE) |
| `anchor_added` | anchor columns (selector as JSON object) + `capture_id` | `anchor` |
| `node_asserted` | `{id, label, props}` | `node`, `node_alias` |
| `edge_asserted` | `{id, src, type, type_node_id, dst, props, valid_from, valid_to}` | `edge` |
| `edge_retracted` | `{id, reason}` (`removed` \| `edited`) | deletes from `edge` + its `support` rows |
| `support_added` | `{subject_kind, subject_id, anchor_id, role}` | `support` |

Removing an edge is an `edge_retracted` event — the projection forgets it,
the log never does. Editing an edge is `edge_retracted` (reason `edited`) +
a fresh `edge_asserted` in one transaction, with the evidence anchors carried
over; the correction itself is the valuable training signal, so it is never
rewritten in place.

`capture_id` groups sibling artifacts from one clipboard capture (one copy
often yields HTML *and* plain text). It lives only in the log — provenance
detail, not graph structure.

## Immutable layer

- **artifact** — `id` is the sha256 of the content; the blob lives at
  `<data_dir>/blobs/<aa>/<sha256>` (never in the database), `path` stores that
  relative path. `provenance` ∈ `exact | sourced | attributed | orphan`
  (see [provenance.md](provenance.md)). `derived_from`/`derivation` are
  reserved for M2 (transcripts, OCR).
- **anchor** — a selector into a region of one artifact. `id` is
  deterministic (`anc_` + sha256 of artifact id + canonical selector JSON), so
  re-capturing the same span is idempotent. M0 implements one selector type:

```json
{"type": "text_position", "start": 123, "end": 456}
```

Offsets are **character** offsets into the artifact's UTF-8-decoded text
(the CF_HTML header speaks in bytes; the parser converts). M1 adds
`text_quote` selectors for anchors that survive page edits.

## Projection

- **node** — `label` is the canonical surface form. Edge-type nodes carry
  `props.kind = "edge_type"`; nothing else distinguishes them, by design
  (types are nodes, not strings). Predicate labels are a controlled ALL_CAPS
  vocabulary ([ADR 0003](adr/0003-predicate-vocabulary.md)): normalized to
  `^[A-Z][A-Z0-9_]*$`, and never created implicitly by asserting an edge.
- **node_alias** — every asserted label is also recorded as an alias;
  entity-resolution merges (M3) extend this table.
- **edge** — `type` holds the edge-type node's *label*, denormalized for
  query speed; the authoritative link is `type_node_id` in the
  `edge_asserted` event. `props.context` carries the optional free-text note.
  `valid_from`/`valid_to` (when the fact was true, vs. observed) are unused
  in M0.
- **support** — evidence for any assertion:
  `role ∈ evidence | commentary | counterexample`. A captured assertion gets
  one `evidence` row pointing at the capture anchor; a manual entry from the
  graph page has none, which the UI surfaces as `evidence = 0` rather than
  hiding.
- **proposal** — the decision record (accepted/rejected/edited/deferred).
  Present in the schema from day one, written from M4. It is *not* cleared by
  replay because proposals are not yet event-sourced.

## Migrations

Numbered `.sql` files in `schema/`, applied in filename order, tracked by
filename in `schema_migration`. Never edit an applied migration; if the
projection shape changes, write a new migration and rely on replay — the
Store replays automatically whenever a new migration lands on a non-empty
log, so a migration file may exist purely to trigger re-projection
(`0003_predicate_vocabulary.sql` is the precedent).
