---
name: schema-migration
description: Scaffold a new numbered schema migration the invariant-safe way — never edit an applied one. Use when adding/changing projection tables, columns, or indexes.
---

# Adding a migration to inspeg

Schema evolution is a NEW numbered file in `schema/`; applied migrations are
never edited (invariant #6, enforced by pre-commit). `Store.__init__`
auto-replays the whole event log after any new migration — "replay, don't
migrate" (ADR 0001).

## Steps

1. **Number**: next `NNNN_short_name.sql` after the highest in `schema/`.
2. **Reshaping an existing projection table?** Use the rebuild pattern from
   `schema/0004_artifact_v2.sql`:
   - `PRAGMA foreign_keys = OFF;` first, `= ON` last;
   - the `_refuse_nonempty_proposal` guard (proposal is NOT replay-rebuilt);
   - empty children before dropping parents; wrap DDL in `BEGIN; … COMMIT;`.
3. **New projection table?** Add it to the DELETE tuple in
   `projection.replay` (src/inspeg/store/projection.py) **in the same
   change** — a table missing there silently survives replay.
4. **New/changed applier?** It must be total: `.get` for new keys, skip
   unknown enum values, never raise (the ADR 0003 two-layer rule). Service
   layer validates; projection normalizes.
5. **Tests** (tests/test_multi_surface.py has templates):
   - replay-idempotence: capture through every writer the change touches,
     `store.replay()`, assert `dump_projection` unchanged;
   - a legacy-payload fixture if the applier derives fields for old events;
   - if a CHECK gained a value, an unknown-value-skipped test.
6. Run `pytest -q` on the full suite; a fresh Store in tests exercises the
   whole migration chain from empty.
7. Note the cost: every shipped migration triggers one O(all events) replay
   at users' next startup — batch migrations within a release when possible.

Finish by running the `replay-verifier` agent over the diff.
