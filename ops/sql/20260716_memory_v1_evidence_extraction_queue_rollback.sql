BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'evidence extraction queue rollback requires sage';
  END IF;
  IF to_regclass('memory.evidence_intake_terminal') IS NOT NULL
     AND EXISTS (
       SELECT 1
       FROM memory.evidence_intake_terminal
       WHERE outcome='dispatched'
     ) THEN
    RAISE EXCEPTION
      'cannot rollback evidence extraction queue with dispatched rows';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.enqueue_owner_evidence_extraction_v1(
  uuid,text,text,text,text
);
DROP TRIGGER IF EXISTS evidence_extraction_event_append_only_guard
  ON memory.evidence_extraction_event;
DROP TRIGGER IF EXISTS evidence_extraction_job_update_guard
  ON memory.evidence_extraction_job;
DROP FUNCTION IF EXISTS
  memory.guard_evidence_extraction_event_append_only();
DROP FUNCTION IF EXISTS
  memory.guard_evidence_extraction_job_update();
DROP TABLE IF EXISTS memory.evidence_extraction_event;
DROP TABLE IF EXISTS memory.evidence_extraction_job;
DROP TYPE IF EXISTS memory.evidence_extraction_job_status;

ALTER TABLE memory.evidence_intake_terminal
  DROP CONSTRAINT evidence_intake_terminal_outcome_check;
ALTER TABLE memory.evidence_intake_terminal
  ADD CONSTRAINT evidence_intake_terminal_outcome_check
  CHECK (outcome IN ('empty','skipped'));

ALTER TABLE memory.evidence_intake_terminal
  DROP CONSTRAINT evidence_intake_terminal_reason_code_check;
ALTER TABLE memory.evidence_intake_terminal
  ADD CONSTRAINT evidence_intake_terminal_reason_code_check
  CHECK (
    reason_code IN (
      'empty_content',
      'missing_content_hash',
      'upstream_completed_empty',
      'upstream_skipped',
      'upstream_review_required',
      'upstream_link_inconsistency'
    )
  );

DO $role$
BEGIN
  IF to_regrole('memory_extraction_queue_maintainer') IS NOT NULL THEN
    DROP OWNED BY memory_extraction_queue_maintainer;
    DROP ROLE memory_extraction_queue_maintainer;
  END IF;
END
$role$;

COMMIT;
