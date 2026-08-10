-- 0006_capture_group.sql
-- Capture grouping: `capture_id` has been in every artifact_added /
-- anchor_added payload since M0 but was never projected. Projecting it now
-- means replay back-fills all history for free. Feeds the HUD's
-- unannotated-capture queue.
--
-- No FK to artifact: rows are cleared and rebuilt by replay alongside it,
-- and a missing artifact row must degrade to a dangling reference, never a
-- replay crash.

BEGIN;

CREATE TABLE capture_member (
  capture_id  TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  anchor_id   TEXT,
  captured_at TEXT,
  PRIMARY KEY (capture_id, artifact_id)
);

CREATE INDEX idx_capture_member_artifact ON capture_member(artifact_id);

COMMIT;
