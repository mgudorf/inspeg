# Contributing

Thanks for your interest. inspeg is early (M0) and the architecture is
deliberate — please read [docs/architecture.md](docs/architecture.md) before
proposing structural changes.

## Setup

```
python -m venv .venv
.venv\Scripts\activate          # or: source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install              # one-time: installs the git pre-commit hooks
pytest
ruff check . && ruff format --check .
```

The pre-commit hooks run gitleaks (blocks commits containing API keys,
tokens, private keys, or other credentials — see `.gitleaks.toml`), a few
hygiene checks, and the same Ruff lint/format that CI enforces. CI also
scans the full git history for secrets on every push and PR.

Windows-only pieces (hotkey, clipboard) are import-guarded; the test suite
must pass on any platform.

## Ground rules

These are architectural constraints, not preferences. PRs that violate them
will be declined regardless of code quality:

1. **No passive capture** (three-part rule, [ADR 0004](docs/adr/0004-ephemeral-display-context.md)).
   Nothing is *persisted* without the user pressing something; nothing
   observes *content* passively (no clipboard listeners, background OCR,
   keystroke hooks, or UIA scraping — ever). The one exception is ephemeral
   display context for the HUD: foreground-window *metadata* and
   extension-pushed tab/workspace identity, in daemon memory only, never
   written to disk, never capture-triggering, disableable with
   `--no-context-watch`.
2. **The event log is append-only.** Never `UPDATE` or `DELETE` events. The
   projection tables are only written via `Store.record` or rebuilt via
   `replay`.
3. **Models propose; only humans assert.** Anything automated writes to
   `proposal`, never to `edge`.
4. **Schema changes are new numbered migrations** in `schema/`; never edit an
   applied migration. If the projection shape changes, replay handles it.
5. **Permissive licenses only** for dependencies (verify at pin time), and no
   blob data in the database — blobs are content-addressed files.

## Practical notes

- Significant design decisions get an ADR in `docs/adr/`.
- Keep capture adapters small, optional, and independently abandonable.
- CI runs Ruff (lint + format) and pytest on Python 3.11–3.13, Ubuntu and
  Windows. Match that locally before pushing.
