# ADR 0006 — The label primitive: one-click topical tagging

- Status: accepted
- Date: 2026-08-08

## Context

Today the smallest unit of annotation is a full triple (`src --[TYPE]--> dst`
via `assert_edge`), which is the structural cause of the ~9-action capture
flow: there is nothing cheaper to say than a complete sentence. The
multi-surface plan needs a one-click primitive — "this capture is about
*AI Knowledge*" — that a context menu can apply with no typing, and that
label-based navigation ("show me everything about X") can query with one
indexed lookup.

## Decision

A **label** is a topic node plus a support row:

- The topic is an ordinary node with `props.kind = "topic"`, created/reused
  via `get_or_create_node`. Topic labels are **free-form entity labels** —
  ADR 0003's ALL_CAPS predicate rule deliberately does not apply (it governs
  edge types only). The UI renders topic chips in a visually distinct style
  so a user-chosen `AI_KNOWLEDGE`-style topic is never mistaken for a
  predicate.
- Applying a label records `support_added {subject_kind: 'node', subject_id:
  <topic node id>, anchor_id, role: 'label'}`. `subject_kind='node'` has been
  legal since `0001_init.sql` and was unused; `role='label'` is added to the
  `support.role` CHECK by `schema/0005_labels.sql`.
- A new event kind **`support_removed`** (same payload shape) makes labels —
  and any future support row — retractable, mirroring `edge_retracted`: the
  log keeps the history, the projection drops the row.
- Queries: label → items is the existing unique-index prefix
  `(subject_kind, subject_id)`; item → labels uses the new
  `idx_support_anchor(anchor_id)`.

## Replay obligations

- Appliers are **total**: `_apply_support_added` validates `role` against the
  set its schema's CHECK accepts and *skips* unknown roles instead of letting
  SQLite raise — so a future role addition never crashes an older binary's
  replay (the ADR 0003 two-layer pattern: service validates and rejects,
  projection normalizes/skips and never raises).
- `support_removed` never appears in pre-0005 logs; old `support_added` rows
  used only the original three roles and replay unchanged.
- Known divergence, documented: an old binary replaying a new log skips
  `support_removed` and keeps rows the newer binary deleted. That is the
  price of the forward-compat hatch and is acceptable for a downgrade.

## Alternatives rejected

- **Overload `role='evidence'`.** "This anchor evidences the concept X" and
  "this anchor is labeled X" are different claims; conflating them poisons
  the evidence semantics of `support` and cannot be undone later without
  reinterpreting the log.
- **A dedicated `tag` table + new event kinds.** More schema, more appliers,
  and it forfeits what `support` already gives: uniqueness, replay, and the
  subject/anchor indexes.
- **Labels as edges to a topic node.** An edge demands a predicate and an
  object — exactly the ceremony the primitive exists to avoid — and would
  flood the edge table with a pseudo-predicate.

## Consequences

- `service.apply_label` / `remove_label` / `list_labels` are the only
  writers/readers; menus feed from `list_labels` (recent = event-log order,
  frequent = `support` GROUP BY).
- A label is a human assertion: `actor="human"` — proposers suggesting labels
  go through `proposal` like everything else (invariant #3).
- Labeled captures become the backbone of the HUD's label view and
  similar-items navigation.
