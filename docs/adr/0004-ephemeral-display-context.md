# ADR 0004 — Ephemeral display context: amending "no passive capture"

- Status: accepted
- Date: 2026-08-08

## Context

The multi-surface plan adds a HUD that regroups captured items by the
*current* context — the foreground application, the active browser tab, the
open VS Code workspace. That is impossible under the original invariant #2 as
worded ("nothing observes the clipboard, screen, or mic unless the user
pressed something; do not add listeners or polling"), which forbids both
`SetWinEventHook` and polling `GetForegroundWindow`, and it forbids the
browser/VS Code extensions pushing "what tab/workspace is focused".

The original invariant conflated two protections that deserve separate
statements: *nothing enters the store passively* (the corpus guarantee) and
*nothing observes content passively* (the surveillance guarantee). The HUD
needs neither weakened — it needs a third, narrow allowance for **display
metadata**.

## Decision

Invariant #2 is restated in three parts. The wording below is normative and
is propagated verbatim in spirit to `CLAUDE.md`, `docs/architecture.md`,
`SECURITY.md`, `CONTRIBUTING.md`, and `src/inspeg/adapters/__init__.py`:

- **(2a) Nothing is persisted without a deliberate user action.** No code
  path may reach `Store.record` — and therefore the event log, the
  projection, the blob store, `cache.db`, or any other file — as a
  consequence of observation alone. Every recorded event must trace to an
  explicit press or click. A regression test asserts that sustained
  observation traffic appends zero events.
- **(2b) Nothing observes content passively.** No clipboard reads, no screen
  pixels, no microphone, no keystroke or low-level input hooks, no
  accessibility/UIA scraping of other applications — whether or not a user
  action is pending.
- **(2c) Sole exception — ephemeral display context.** The daemon may observe
  foreground-window **identity metadata** (process image name, window handle,
  window title) via win32 foreground/name-change events, and may accept the
  active tab URL/title or workspace path **pushed by the user-installed
  browser/IDE extension**, solely to filter, group, and highlight in the HUD.
  This context:
  - lives only in daemon process memory with a bounded lifetime;
  - is never written to `inspeg.db`, the event log, any file, or any log line;
  - is never transmitted off-host (loopback service of `GET /api/context` to
    same-user processes is the documented, tested V16 risk escalation);
  - is never used to trigger, time, or enrich a capture — capture-time source
    attribution continues to come only from the capture gesture itself;
  - is gated behind one master toggle (`--no-context-watch`, surfaced in
    `/api/health` and visibly in the HUD) which, when off, **uninstalls the
    hooks and refuses the push endpoints** rather than merely hiding output.

  Anything not explicitly permitted by (2c) remains forbidden under (2a) and
  (2b).

## Consequences

- `docs/security.md` gains **V16**: `GET /api/context` escalates the accepted
  local-process risk from "any local process can read what you captured" to
  "…what you are doing right now". Window titles are quasi-content (document
  names, mail subjects); the toggle bounds this, it does not eliminate it.
- The no-persistence guarantee is a tested control, per the security-doc
  doctrine: context churn must append zero events and leave `inspeg.db`
  untouched.
- With `--no-context-watch`, the HUD falls back to on-demand sampling of the
  foreground window at hotkey/HUD-open time — the original, uncontroversial
  model.
- The "use it fifty times before M1" gate in `docs/architecture.md` §9 is
  consciously overridden by the whole-system directive; the dogfood gate
  moves to the end of the multi-surface build.
