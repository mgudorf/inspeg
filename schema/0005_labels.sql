-- 0005_labels.sql
-- The label primitive (ADR 0006): support.role gains 'label', and the
-- item -> labels direction gets its index. `support` was emptied by 0004 and
-- is replay-rebuilt, so drop-and-recreate is safe and lossless.

BEGIN;

DROP TABLE support;

CREATE TABLE support (
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('node', 'edge')),
  subject_id   TEXT NOT NULL,
  anchor_id    TEXT NOT NULL REFERENCES anchor(id),
  role         TEXT NOT NULL CHECK (role IN ('evidence', 'commentary', 'counterexample', 'label'))
);

CREATE UNIQUE INDEX idx_support_unique ON support(subject_kind, subject_id, anchor_id, role);
CREATE INDEX idx_support_anchor ON support(anchor_id);

COMMIT;
