-- 0002_artifact_redaction.sql
-- Redaction: the one sanctioned way to destroy captured content (e.g. a
-- password captured by accident). The blob file is deleted; the artifact row
-- is flagged so the API stops serving its content; the event log keeps an
-- artifact_redacted event so replay reproduces the flag. See docs/adr/0002.

ALTER TABLE artifact ADD COLUMN redacted INTEGER NOT NULL DEFAULT 0;
