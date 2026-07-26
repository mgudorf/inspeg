-- 0001_init.sql
-- Immutable layer, event log, decision record, and projection.
-- The event log is the database of record; artifact/anchor/node/edge/support
-- are a materialized projection rebuilt by replaying it (see docs/adr/0001).

-- ── Immutable layer ─────────────────────────────────────────────────────────

CREATE TABLE artifact (
  id            TEXT PRIMARY KEY,               -- sha256 of content
  mimetype      TEXT NOT NULL,
  byte_len      INTEGER NOT NULL,
  path          TEXT NOT NULL,                  -- blobs/<aa>/<sha256>, relative to the data dir
  captured_at   TEXT NOT NULL,                  -- ISO8601, capture time
  provenance    TEXT NOT NULL CHECK (provenance IN ('exact', 'sourced', 'attributed', 'orphan')),
  source_uri    TEXT,                           -- URL if known
  source_app    TEXT,                           -- exe / window title if known
  derived_from  TEXT REFERENCES artifact(id),   -- transcript_of, ocr_of
  derivation    TEXT                            -- null for originals
);

CREATE TABLE anchor (
  id            TEXT PRIMARY KEY,
  artifact_id   TEXT NOT NULL REFERENCES artifact(id),
  selector_type TEXT NOT NULL,                  -- text_quote | text_position | media_frag | bbox
  selector      TEXT NOT NULL                   -- JSON; shape depends on type
);

CREATE INDEX idx_anchor_artifact ON anchor(artifact_id);

-- ── Event log (append-only; never UPDATE or DELETE) ─────────────────────────

CREATE TABLE event (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  kind       TEXT NOT NULL,
  payload    TEXT NOT NULL,                     -- JSON
  actor      TEXT NOT NULL                      -- 'human' | 'proposer:<name>'
);

CREATE INDEX idx_event_kind ON event(kind);

-- ── Decision record ──────────────────────────────────────────────────────────

CREATE TABLE proposal (
  id           TEXT PRIMARY KEY,
  proposer     TEXT NOT NULL,
  anchor_id    TEXT REFERENCES anchor(id),
  proposed     TEXT NOT NULL,                   -- JSON: the suggested node/edge/type
  disposition  TEXT NOT NULL CHECK (disposition IN ('accepted', 'rejected', 'edited', 'deferred')),
  final        TEXT,                            -- JSON: what the human actually asserted
  decided_at   TEXT
);

-- ── Projection ───────────────────────────────────────────────────────────────

CREATE TABLE node (
  id      TEXT PRIMARY KEY,
  label   TEXT NOT NULL,                        -- canonical surface form
  props   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_node_label ON node(label);

CREATE TABLE node_alias (
  node_id TEXT NOT NULL REFERENCES node(id),
  surface TEXT NOT NULL,
  PRIMARY KEY (node_id, surface)
);

CREATE TABLE edge (
  id         TEXT PRIMARY KEY,
  src        TEXT NOT NULL REFERENCES node(id),
  type       TEXT NOT NULL,                     -- label of the edge-type node, denormalized
  dst        TEXT NOT NULL REFERENCES node(id),
  props      TEXT NOT NULL DEFAULT '{}',
  valid_from TEXT,
  valid_to   TEXT                               -- when the fact was true (vs. observed)
);

CREATE INDEX idx_edge_src_type ON edge(src, type);
CREATE INDEX idx_edge_dst_type ON edge(dst, type);

CREATE TABLE support (                          -- evidence for any assertion
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('node', 'edge')),
  subject_id   TEXT NOT NULL,
  anchor_id    TEXT NOT NULL REFERENCES anchor(id),
  role         TEXT NOT NULL CHECK (role IN ('evidence', 'commentary', 'counterexample'))
);

CREATE UNIQUE INDEX idx_support_unique ON support(subject_kind, subject_id, anchor_id, role);
