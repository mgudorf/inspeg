# ADR 0003 — Predicates are a controlled ALL_CAPS vocabulary

- Status: accepted
- Date: 2026-07-26

## Context

Architecture §10.2 names predicate proliferation as a known killer: "You will
invent forty near-synonymous edge types in a month. Mitigation: edge types are
nodes too; make creating a new one require one extra click." Freeform
predicate entry also invites typo-variants (`instance_of` / `instance of` /
`InstanceOf`) that silently fragment the graph.

## Decision

1. **Predicate labels are normalized identifiers**: whitespace/hyphen runs
   collapse to `_`, uppercased, and must match `^[A-Z][A-Z0-9_]*$`
   (`instance of` → `INSTANCE_OF`). The ALL_CAPS form makes predicates
   visually distinct from entity labels everywhere they appear.
2. **Asserting an edge cannot create a predicate implicitly.** The edge type
   must already exist as an edge-type node; creating one is a separate,
   deliberate action (`POST /api/predicates`, or `create_predicate: true` on
   the assertion — the UI turns this into an explicit confirmation click).
3. **Normalization lives in two layers.** The service layer validates and
   rejects (`service.normalize_predicate`); the projection layer normalizes
   mechanically without ever raising (`util.normalize_predicate_label`), so
   replaying pre-vocabulary events re-projects old lowercase labels into the
   normalized form instead of crashing.

Entity labels are deliberately untouched: surface forms are evidence, and
entity resolution is the alias table's job (M3), not a casing rule's.

## Consequences

- Migration `0003_predicate_vocabulary.sql` changes no schema; it exists so
  the Store detects a new migration and replays, re-projecting existing data.
  This established the pattern: **a new migration on an existing log always
  triggers a replay** (`Store.__init__`), which is §3.2's "replay, don't
  migrate" made automatic.
- The event log keeps whatever label was asserted at the time; only the
  projection is normalized. Two pre-vocabulary nodes whose labels collide
  after normalization (`rel` and `REL`) remain distinct nodes with one label —
  merging them is an M3 alias/merge decision, recorded like any other.
- The quick-capture flow gains exactly one extra click for a new predicate,
  none for an existing one (autocomplete from `GET /api/predicates`).
