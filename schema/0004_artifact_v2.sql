-- 0004_artifact_v2.sql
-- Pointer artifacts (ADR 0005) + context identity columns (ADR 0008).
--
-- The projection is rebuilt by replay after any new migration
-- (Store.__init__), so this script reshapes `artifact` by dropping and
-- recreating it rather than migrating rows. Children are emptied first so no
-- foreign-key violation can exist at any point; `proposal` is guarded below
-- because it is NOT replay-rebuilt (ADR 0001) and holds an FK to anchor.

PRAGMA foreign_keys = OFF;

-- Refuse to run if `proposal` has rows: emptying `anchor` would orphan them,
-- and replay would not bring them back. Inserting NULL into a NOT NULL column
-- aborts the whole script with a clear IntegrityError; a proposal-preserving
-- rebuild must then be written as its own migration.
CREATE TEMP TABLE _refuse_nonempty_proposal (proposal_must_be_empty INTEGER NOT NULL);
INSERT INTO _refuse_nonempty_proposal SELECT NULL FROM proposal LIMIT 1;
DROP TABLE _refuse_nonempty_proposal;

BEGIN;

-- Children before parents; replay repopulates all of these.
DELETE FROM support;
DELETE FROM edge;
DELETE FROM node_alias;
DELETE FROM node;
DELETE FROM anchor;
DROP TABLE artifact;

CREATE TABLE artifact (
  id              TEXT PRIMARY KEY,   -- sha256 (blob) | 'pt_'+sha256(canonical_json({kind,target})) (pointer)
  kind            TEXT NOT NULL DEFAULT 'blob' CHECK (kind IN ('blob', 'pointer')),
  mimetype        TEXT NOT NULL,
  byte_len        INTEGER,            -- NULL allowed for pointers (reported size lives in locator)
  path            TEXT CHECK ((kind = 'blob') = (path IS NOT NULL)),
  locator         TEXT,               -- JSON pointer descriptor: {kind, target, byte_len?, mtime?, content_sha256?}
  captured_at     TEXT NOT NULL,
  provenance      TEXT NOT NULL CHECK (provenance IN ('exact', 'sourced', 'attributed', 'orphan')),
  source_uri      TEXT,               -- raw, display-only provenance (V4 discipline)
  source_uri_norm TEXT,               -- util.normalize_source_uri (ADR 0008)
  source_exe      TEXT,               -- 'chrome.exe'
  source_title    TEXT,
  context_key     TEXT,               -- 'url:...' | 'file:...' | 'workspace:...' | 'app:...' | NULL
  source_app      TEXT,               -- legacy composite, kept for display compatibility
  derived_from    TEXT REFERENCES artifact(id),
  derivation      TEXT,
  redacted        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_artifact_uri_norm    ON artifact(source_uri_norm);
CREATE INDEX idx_artifact_context_key ON artifact(context_key);
CREATE INDEX idx_artifact_captured_at ON artifact(captured_at);
CREATE INDEX idx_artifact_kind        ON artifact(kind);

COMMIT;

PRAGMA foreign_keys = ON;
