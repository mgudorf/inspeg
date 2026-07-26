# ADR 0001 — Append-only event log; graph is a projection

- Status: accepted
- Date: 2026-07-26

## Context

The graph schema will change several times in the first month of real use.
Migrating mutable node/edge tables on every change is expensive and lossy —
in particular, it destroys the history of *how* the graph was built, and that
history (corrections, rejections, merges) is the part of the corpus that has
training value. Accepted triples alone are reproducible by any competent LLM;
the decision record is not.

## Decision

The database of record is an ordered, append-only log of immutable events
(`event` table: `seq`, `ts`, `kind`, `payload`, `actor`). The
artifact/anchor/node/edge/support tables are a materialized projection rebuilt
by replaying the log (`inspeg.store.projection.replay`).

Consequences enforced in code:

- All writes go through `Store.record`, which appends the event and applies
  it to the projection in one transaction. Nothing writes projection tables
  directly.
- Events are never updated or deleted. Corrections are new events.
- `actor` (`human` vs `proposer:<name>`) is recorded on every event; it is
  what keeps the corpus trainable rather than circular.
- Unknown event kinds are skipped by the projection, so the log may carry
  richer history than any given projection consumes.
- Schema evolution = new numbered migration in `schema/` + replay. No data
  migrations of projected tables.

## Consequences

- Bitemporality is free: *when observed* comes from log order, *when true*
  from `valid_from`/`valid_to` on edges.
- The training-data corpus **is** the log, not the projection.
- Replay must stay correct as event kinds are added; M3 makes replay a tested
  invariant before the first projection-schema change lands.
- The `proposal` table is not yet event-sourced (that lands with proposers in
  M4), so replay deliberately leaves it untouched.
