# inspeg

Manual, multimodal capture into a **provenance-anchored knowledge graph**.

You deliberately capture fragments of things you encounter — web pages, code,
your own notes — and assert typed relationships between them, such that every
assertion points back at the exact evidence that produced it. The output is a
knowledge graph you can query **and** a provenance-complete record of how you
built it (including what you rejected), which is the part with value as
training data.

Local-first: single user, single machine, SQLite plus files on disk. No cloud,
no accounts, no passive capture — every artifact enters the system because you
pressed something.

> Status: **M0 — vertical slice.** Hotkey → CF_HTML parse → artifact + anchor →
> one typed edge → SQLite, with a quick-capture window. See
> [docs/architecture.md](docs/architecture.md) for the full plan and roadmap.

## 60-second demo

```
pip install -e .
inspeg
```

1. The daemon starts on `http://127.0.0.1:8137` and registers the global
   hotkey **Win+Shift+A** (Windows).
2. Copy something in any app — a paragraph from your browser, a line from an
   editor.
3. Press **Win+Shift+A**. The quick-capture window opens showing what you
   captured, its provenance tier, and the source URL when the clipboard
   carried one.
4. Type a triple — `SQLite` —`has_license`→ `Public Domain` — and hit
   **Assert edge**. The edge lands in the graph with the captured span as
   evidence.

Everything is stored under `~/.inspeg/`: an append-only event log plus
projection in `inspeg.db`, and content-addressed blobs under `blobs/`. Any
language, any decade, no API required.

Captured something you shouldn't have? `POST /api/artifacts/<sha256>/redact`
destroys the content and keeps the provenance record
([ADR 0002](docs/adr/0002-redaction.md)).

The default hotkey is **Win+Shift+A** rather than Ctrl+Alt+A because Windows
synthesizes AltGr as Ctrl+Alt — a Ctrl+Alt hotkey fires while people on
non-US keyboard layouts are typing. Override with `--hotkey`.

## Provenance tiers

Every artifact records how well it can be traced back:

| Tier | Name | What you have | M0 source |
|---|---|---|---|
| 1 | `exact` | URL + char offsets + content hash | browser extension (M1) |
| 2 | `sourced` | `SourceURL` from CF_HTML + fragment HTML | clipboard from a browser |
| 3 | `attributed` | app name + window title + timestamp | clipboard from anywhere else |
| 4 | `orphan` | blob + timestamp only | bare text, unknown origin |

## Development

```
python -m venv .venv
.venv\Scripts\activate          # or: source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

The capture hotkey and clipboard adapter are Windows-only (pywin32); the core
(parsing, storage, API, UI) is platform-neutral and fully tested on any OS.

Run without the hotkey (any platform): `inspeg --no-hotkey`.

## Repository layout

```
src/inspeg/
├── store/      # blobstore, event log, projection, replay
├── model/      # pydantic schemas, selector types
├── api/        # FastAPI routes
├── adapters/   # CF_HTML parser, clipboard, hotkey
└── service.py  # ingestion + assertions (the only writers)
schema/         # numbered .sql migrations
ui/             # quick-capture window (vanilla JS)
docs/           # architecture, data model, provenance, security, ADRs
tests/
```

## Security

The daemon is loopback-only and unauthenticated, which means the browser is
the threat: any page you visit can reach `127.0.0.1:8137`. inspeg enforces
same-origin and a loopback `Host` allowlist to close CSRF and DNS-rebinding
attacks, and refuses to bind a non-loopback interface without an explicit
`--allow-remote`.

- [SECURITY.md](SECURITY.md) — the threat model, and what is *not* defended.
- [docs/security.md](docs/security.md) — every patched vector, its control,
  and the test that fails if the control is removed.

Secrets are kept out of the repository by a gitleaks pre-commit hook and a CI
job that scans the full history; run `pre-commit install` after cloning.

## Roadmap

- **M0** — vertical slice (this) · *gate: use it fifty times*
- **M1** — browser extension, tier-1 anchoring
- **M2** — audio commentary + screenshots
- **M3** — graph view, replay-from-log tested
- **M4** — proposers (models propose, only humans assert)
- **M5** — W3C Web Annotation export + training dumps

## License

[Apache-2.0](LICENSE). See [SECURITY.md](SECURITY.md) for the threat model —
this tool reads the clipboard on demand and stores blobs unencrypted on disk;
you should know exactly what that means before using it.
