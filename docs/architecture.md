# Architecture Plan

> Working name: **anchor** — manual, multimodal capture → provenance-anchored knowledge graph.
>
> *Shipped as **inspeg**; "anchor" below is the retained working name. The anchor **concept** (a selector into a region of an artifact) is unrelated and unchanged.*

## 1. What this is

A local-first tool for **deliberately** capturing fragments of things you encounter (web pages, code, your own dashboards, your own voice) and asserting typed relationships between them, such that every assertion points back at the exact evidence that produced it.

The output is two things at once:

1. A knowledge graph you can query.
2. A provenance-complete record of **how you built it** — including what you rejected — which is the part that has value as training data.

## 2. Non-goals

Explicit, because each of these doubles the project:

- **No passive/background capture.** No clipboard daemon, no screen recording, no ambient OCR. Every artifact enters the system because the user pressed something. This is a hard architectural constraint, not a phase-1 simplification.
- **No automatic extraction as source of truth.** Models may *propose*; only a human *asserts*. A proposal that was never accepted is still recorded (see §6.3), but it is not an edge.
- **No cloud, no accounts, no sync** in v1. Single user, single machine.
- **No general-purpose PKM.** Not competing with Obsidian. No editor, no daily notes, no wiki.
- **No custom graph database.** SQLite until proven insufficient.

## 3. Core design decisions

### 3.1 Modality is a property of evidence, not of the graph

The graph layer never learns what a WAV file is. Four tables carry everything:

- **Artifact** — an immutable blob + mimetype, content-addressed. Text, audio, image, code, PDF.
- **Anchor** — a selector into a region of one artifact. Char offsets for text, `t=start,end` for audio, bbox for images.
- **Node / Edge** — typed graph elements. Each carries a list of supporting anchor IDs.

Adding a modality = writing a capture adapter and a selector type. It does not touch the graph schema.

### 3.2 Append-only event log; graph is a projection

The database of record is an ordered log of immutable events (`artifact_added`, `node_asserted`, `edge_asserted`, `nodes_merged`, `proposal_rejected`, …). The node/edge tables are a materialized projection rebuilt by replaying the log.

Why this matters more than it looks:
- Schema will change three times in the first month. Replay, don't migrate.
- Bitemporality is free: *when observed* vs *when true* fall out of the log ordering.
- The training-data corpus **is** the log. Not the projection.

### 3.3 Provenance is tiered and explicit

Every artifact records how well it can be traced back. Stored as a column, filterable.

| Tier | Name | What you have | Source |
|---|---|---|---|
| 1 | `exact` | URL + char offsets + content hash | Browser extension |
| 2 | `sourced` | `SourceURL` from CF_HTML + fragment HTML | Clipboard from a browser |
| 3 | `attributed` | App name + window title + timestamp | Clipboard from anywhere else |
| 4 | `orphan` | Blob + timestamp only | Screenshot, mic, file drop |

Tier 4 is fine as commentary and useless as evidence. Recording the weakness beats discovering it during training.

### 3.4 Types are nodes, not strings

`Microsoft Corp. --[instance_of]--> Tech Company` where `Tech Company` is a real node. No free-text label column doing double duty. Property-graph labels are denormalized *into* the projection for query speed but are derived, never authored.

Free-text observations that resist a clean predicate go on a `context` property, not into an edge. Resist the temptation to make this a dumping ground.

## 4. Language & stack

**Python for everything except the browser extension.** The extension is JavaScript because there is no alternative; it is deliberately thin (capture and POST, nothing else).

No Rust, no Electron, no desktop toolkit. The UI is a local web app served by the same process that owns the database.

The "Python can integrate later" requirement is satisfied structurally rather than by choice of framework: **the storage format is SQLite plus files on disk.** Any language, any decade, no API required.

### 4.1 Dependencies

All permissive open source. Verify licenses at pin time — they change.

**Core (required)**

| Purpose | Library | License |
|---|---|---|
| Storage | SQLite (stdlib `sqlite3`) | Public domain |
| API + local UI serving | FastAPI | MIT |
| ASGI server | Uvicorn | BSD-3-Clause |
| Schema validation | Pydantic | MIT |
| Hashing | `hashlib` (stdlib) | PSF |
| HTML fragment parsing | lxml *or* BeautifulSoup4 | BSD-3 / MIT |
| Windows hotkey + clipboard | pywin32 (`RegisterHotKey`, `win32clipboard`) | PSF |
| Tests | pytest | MIT |
| Lint/format | Ruff | MIT |
| Packaging | Hatchling | MIT |

**Capture adapters (add as built)**

| Purpose | Library | License |
|---|---|---|
| Audio capture | sounddevice (PortAudio) | MIT |
| Screenshot | mss | MIT |
| Local ASR | faster-whisper | MIT |

**Optional / later**

| Purpose | Library | License |
|---|---|---|
| Graph visualization | Cytoscape.js | MIT |
| Fuzzy candidate generation | RapidFuzz | MIT |
| Embeddings for entity resolution | sentence-transformers | Apache-2.0 |
| Local model proposals | Ollama / llama.cpp | MIT |
| OCR fallback | RapidOCR *or* Tesseract | Apache-2.0 |
| Embedded graph DB (projection target) | Kùzu | MIT |
| Analytics over the log | DuckDB | MIT |

**Vendored, not reimplemented**

- **`dom-anchor-text-quote`** (MIT) or **`apache-annotator`** (Apache-2.0) — robust text anchoring that survives page edits. This looks trivial and is not. Never write it yourself.
- **W3C Web Annotation Data Model** — taken as a spec, not an implementation. Already defines selectors for text, image, and media fragments.

**Explicitly avoided**

- **Neo4j Community (GPLv3)** — copyleft, incompatible with a permissive public repo. Kùzu (MIT) covers the same need embedded.
- **Qt / PySide (LGPL)** — avoidable entirely; the UI is a web page.
- **Model weights** — note that a model's license is separate from its runtime's. Pin and document both.

## 5. System shape

```
┌──────────────────────────────────────────────────────┐
│  Capture adapters (each is small, each is optional)  │
│                                                      │
│  browser-ext ─┐   hotkey+clipboard ─┐                │
│  push-to-talk ┤   screenshot ───────┤                │
│  dashboard ───┘   file drop ────────┘                │
└──────────────┬───────────────────────────────────────┘
               │  HTTP POST → 127.0.0.1:<port>
               ▼
┌──────────────────────────────────────────────────────┐
│  anchor-core  (single Python process)                │
│                                                      │
│   ingest → blobstore (content-addressed files)       │
│          → event log (append-only, SQLite)           │
│          → projection (nodes/edges/anchors, SQLite)  │
│                                                      │
│   proposers (optional, out-of-band, never authoritative)
└──────────────┬───────────────────────────────────────┘
               │  serves
               ▼
        annotation UI (local web app)
```

**Single process, single port.** The daemon owns the database. Adapters are dumb clients that speak one JSON endpoint. This is why capture doesn't die when the dashboard is closed, and why the dashboard can post structured captures *directly* rather than round-tripping through the clipboard.

One process per data directory is enforced by an OS lock on `<data-dir>/.lock`: two daemons sharing a data dir race the projection and the blobstore. The port is loopback-only and unauthenticated, which makes the *browser* the threat model — every page the user visits can reach it — so the API enforces same-origin and a loopback `Host` allowlist. See [security.md](security.md).

## 6. Data model

Sketch, not final. SQL because SQLite is the format of record.

### 6.1 Immutable layer

```sql
CREATE TABLE artifact (
  id            TEXT PRIMARY KEY,   -- sha256 of content
  mimetype      TEXT NOT NULL,
  byte_len      INTEGER NOT NULL,
  path          TEXT NOT NULL,      -- blobs/<aa>/<sha256>
  captured_at   TEXT NOT NULL,      -- ISO8601, capture time
  provenance    TEXT NOT NULL,      -- exact|sourced|attributed|orphan
  source_uri    TEXT,               -- URL if known
  source_app    TEXT,               -- exe / window title if known
  derived_from  TEXT REFERENCES artifact(id),  -- transcript_of, ocr_of
  derivation    TEXT,               -- null for originals
  redacted      INTEGER NOT NULL DEFAULT 0     -- content destroyed; ADR 0002
);

CREATE TABLE anchor (
  id            TEXT PRIMARY KEY,
  artifact_id   TEXT NOT NULL REFERENCES artifact(id),
  selector_type TEXT NOT NULL,      -- text_quote|text_position|media_frag|bbox
  selector      TEXT NOT NULL       -- JSON; shape depends on type
);
```

Blobs live on disk under `blobs/<first-two-hex>/<sha256>`, never in the database. Deduplication is automatic and free.

Blobs are immutable with exactly one exception: **redaction** ([ADR 0002](adr/0002-redaction.md)) destroys an artifact's content — for the password you copied by accident — while keeping its provenance skeleton, by appending an `artifact_redacted` event rather than rewriting the log.

### 6.2 Event log

```sql
CREATE TABLE event (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  kind       TEXT NOT NULL,
  payload    TEXT NOT NULL,   -- JSON
  actor      TEXT NOT NULL    -- 'human' | 'proposer:<name>'
);
```

`actor` is not decoration. It is the column that separates your judgments from a model's suggestions, and it is what makes the corpus trainable rather than circular.

### 6.3 The decision record

The highest-value table, and the one no existing tool gives you:

```sql
CREATE TABLE proposal (
  id           TEXT PRIMARY KEY,
  proposer     TEXT NOT NULL,
  anchor_id    TEXT REFERENCES anchor(id),
  proposed     TEXT NOT NULL,     -- JSON: the suggested node/edge/type
  disposition  TEXT NOT NULL,     -- accepted|rejected|edited|deferred
  final        TEXT,              -- JSON: what the human actually asserted
  decided_at   TEXT
);
```

An "accepted triples" table is reproducible by any competent LLM given the same source text — near-zero marginal value. Your **corrections, rejections, and merges** are not reproducible. Log them from day one; they cannot be reconstructed later.

### 6.4 Projection

```sql
CREATE TABLE node (
  id      TEXT PRIMARY KEY,
  label   TEXT NOT NULL,          -- canonical surface form
  props   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE node_alias (
  node_id TEXT NOT NULL REFERENCES node(id),
  surface TEXT NOT NULL,
  PRIMARY KEY (node_id, surface)
);

CREATE TABLE edge (
  id       TEXT PRIMARY KEY,
  src      TEXT NOT NULL REFERENCES node(id),
  type     TEXT NOT NULL,
  dst      TEXT NOT NULL REFERENCES node(id),
  props    TEXT NOT NULL DEFAULT '{}',
  valid_from TEXT, valid_to TEXT   -- when the fact was true (vs. observed)
);

CREATE TABLE support (                -- evidence for any assertion
  subject_kind TEXT NOT NULL,         -- 'node' | 'edge'
  subject_id   TEXT NOT NULL,
  anchor_id    TEXT NOT NULL REFERENCES anchor(id),
  role         TEXT NOT NULL          -- evidence | commentary | counterexample
);
```

`support.role = 'commentary'` is where a spoken audio note attaches to an edge. `counterexample` is how contradicting evidence gets recorded without deleting anything.

Indexes on `edge(src, type)` and `edge(dst, type)` make traversal fast enough that a graph database is unnecessary well past 10⁷ edges — a scale unreachable by hand annotation.

## 7. Capture adapters

Each is independently useful and independently abandonable.

### 7.1 Browser extension (MV3, JavaScript) — **build first**

Highest provenance tier, and the only way to get real character offsets.

- `chrome.contextMenus` with `contexts: ["selection"]`; menu creation lives in the service worker.
- A content script listens for `contextmenu`, captures `window.getSelection().getRangeAt(0)` plus `cloneContents()` **before** the menu fires. `info.selectionText` is plain text and will silently destroy every `href` in the selection — do not rely on it.
- Two-stage flow enforced by the UI: first selection creates a **Document** artifact (URL + full text + content hash); subsequent selections are validated as offsets *inside* that artifact. This is what buys provenance rather than bolting it on.
- Anchors stored as `TextQuoteSelector` (exact + prefix/suffix), not DOM paths.

### 7.2 Hotkey clipboard

Manual by construction: user copies, then presses the hotkey. No listener, no `WM_CLIPBOARDUPDATE`, nothing observes the clipboard unless invoked.

- `RegisterHotKey` via pywin32. On fire, enumerate **all** available clipboard formats and store each as a sibling artifact — one copy often yields `HTML Format`, `CF_UNICODETEXT`, and `CF_DIB` at once. That is a free multimodal capture.
- Parse the `CF_HTML` description header for `SourceURL`, `StartFragment`/`EndFragment`. When present → tier 2, with `href`s intact. When absent → `GetForegroundWindow` for process name and title → tier 3.

### 7.3 Push-to-talk commentary

- Hold hotkey → record via sounddevice → WAV artifact, tier 4.
- Transcribe with faster-whisper into a **derived** artifact (`derivation = 'transcript_of'`). The audio stays authoritative; swapping ASR models re-derives without touching the graph.
- Attaches as `support.role = 'commentary'` on whatever was selected.

### 7.4 Dashboard direct

Your quiz Q/A and code snippets are already structured. POST artifacts with anchors pre-populated. Serializing them to the clipboard and re-parsing is strictly lossy — don't.

## 8. Repository layout

```
anchor/
├── README.md                  # what it is, 60-second demo, install
├── LICENSE                    # Apache-2.0
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── SECURITY.md                # non-optional: this tool touches the clipboard
├── CHANGELOG.md
├── pyproject.toml
├── .github/
│   └── workflows/ci.yml       # ruff + pytest, 3.11/3.12/3.13
├── docs/
│   ├── architecture.md        # this file
│   ├── data-model.md
│   ├── provenance.md          # the tier system, in depth
│   └── adr/                   # 0001-append-only-log.md, ...
├── src/anchor/
│   ├── store/                 # blobstore, event log, projection, replay
│   ├── model/                 # pydantic schemas, selector types
│   ├── api/                   # FastAPI routes
│   ├── adapters/              # clipboard, audio, screenshot, filedrop
│   ├── proposers/             # optional, pluggable, never authoritative
│   └── export/                # web-annotation JSON-LD, training dumps
├── ui/                        # local web app (vanilla or minimal build)
├── extension/                 # MV3 browser extension
├── schema/                    # numbered .sql migrations
└── tests/
```

**Why Apache-2.0:** explicit patent grant, permissive enough that the extension and any future forks stay unencumbered, and compatible with every dependency above.

**SECURITY.md is required, not boilerplate.** A tool that reads the clipboard on demand and stores blobs unencrypted on disk needs a stated threat model and a stated non-model. Say plainly: local-only, no network egress by default, blobs unencrypted at rest, and the user is responsible for what they capture.

## 9. Milestones

**M0 — Vertical slice (target: one week).** Hotkey → CF_HTML parse → artifact + anchor → one typed edge → SQLite. Quick-capture window only, no graph view. *Gate: use it fifty times. If it doesn't feel good, no amount of architecture fixes it.*

**M1 — Provenance.** Browser extension, tier-1 anchoring with `dom-anchor-text-quote`, document-then-span enforcement, re-highlight on revisit.

**M2 — Multimodal.** Audio commentary + transcript derivation. Screenshot artifacts. Proves the Artifact/Anchor abstraction actually holds.

**M3 — Graph usable.** Cytoscape.js view, alias table, manual node merge. Replay-from-log implemented and tested — this is the insurance policy for every schema change after it.

**M4 — Proposers.** Optional local-model suggestions writing to `proposal`, never to `edge`. Gazetteer auto-highlight of known surface forms — the single highest-leverage speedup in the whole system.

**M5 — Export.** W3C Web Annotation JSON-LD; a training dump that includes the decision record, not just the accepted graph.

## 10. Known hard problems

Listed so they're not discovered as surprises.

1. **Entity resolution.** `MSFT` / `Microsoft Corp.` / `Microsoft` / `$MSFT`. This is where these systems die. Mitigation: never auto-merge. Propose merges, log the decision, keep an alias table. The merge decisions are themselves valuable data.
2. **Predicate proliferation.** You will invent forty near-synonymous edge types in a month. Mitigation: edge types are nodes too; make creating a new one require one extra click, and periodically review the long tail.
3. **Anchor rot.** Source pages change or vanish. Mitigation: always store the full document artifact with a content hash, not just the span. The quote selector re-locates; the hash tells you when re-location is untrustworthy.
4. **Corpus scale honesty.** Hand annotation produces 10²–10⁴ examples. That is an eval set, a retrieval corpus, preference data, or ground truth for distilling an automatic labeler. It is not a pretraining corpus. Design the export formats for those uses specifically.
5. **Cross-modal alignment.** Multiple modalities only pay off if they're aligned on the *same referent*, not merely co-occurring in a session. Making the anchor — not the session, not the document — the unit of alignment is what keeps this queryable later.
