# Contributing

Thanks for your interest. inspeg is early (M0) and the architecture is
deliberate — please read [docs/architecture.md](docs/architecture.md) before
proposing structural changes.

## Setup

```
python -m venv .venv
.venv\Scripts\activate          # or: source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

Windows-only pieces (hotkey, clipboard) are import-guarded; the test suite
must pass on any platform.

## Ground rules

These are architectural constraints, not preferences. PRs that violate them
will be declined regardless of code quality:

1. **No passive capture.** Every artifact enters the system because the user
   pressed something. No clipboard listeners, no background OCR, no daemons
   that watch.
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
