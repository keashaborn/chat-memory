BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'evidence extraction worker rollback requires sage';
  END IF;
  IF to_regclass('memory.evidence_extraction_event') IS NOT NULL
     AND EXISTS (
       SELECT 1
       FROM memory.evidence_extraction_event
       WHERE operation_id IS NOT NULL
     ) THEN
    RAISE EXCEPTION
      'cannot rollback evidence extraction worker with lifecycle events';
  END IF;
  IF to_regclass('memory.evidence_extraction_job') IS NOT NULL
     AND EXISTS (
       SELECT 1
       FROM memory.evidence_extraction_job
       WHERE checkpoint_sequence<>0
          OR checkpoint_sha256 IS NOT NULL
     ) THEN
    RAISE EXCEPTION
      'cannot rollback evidence extraction worker with checkpoints';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.resolve_owner_evidence_extraction_review_v1(
  uuid,uuid,text,text,text,jsonb
);
DROP FUNCTION IF EXISTS memory.fail_owner_evidence_extraction_job_v1(
  uuid,uuid,uuid,text,text,text,text,integer
);
DROP FUNCTION IF EXISTS memory.finish_owner_evidence_extraction_job_v1(
  uuid,uuid,uuid,text,text,text,text,jsonb
);
DROP FUNCTION IF EXISTS memory.checkpoint_owner_evidence_extraction_job_v1(
  uuid,uuid,uuid,text,text,integer,text,jsonb,integer
);
DROP FUNCTION IF EXISTS memory.claim_owner_evidence_extraction_job_v1(
  uuid,text,text,integer,integer
);

DROP INDEX IF EXISTS
  memory.evidence_extraction_event_owner_operation_uidx;
ALTER TABLE memory.evidence_extraction_event
  DROP COLUMN IF EXISTS operation_id;

ALTER TABLE memory.evidence_extraction_job
  DROP CONSTRAINT IF EXISTS
    evidence_extraction_job_checkpoint_binding_check,
  DROP CONSTRAINT IF EXISTS
    evidence_extraction_job_checkpoint_sequence_check,
  DROP COLUMN IF EXISTS checkpoint_sha256,
  DROP COLUMN IF EXISTS checkpoint_sequence;

CREATE OR REPLACE FUNCTION memory.guard_evidence_extraction_job_update()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=pg_catalog
AS $function$
BEGIN
  IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
     OR NEW.job_id IS DISTINCT FROM OLD.job_id
     OR NEW.evidence_id IS DISTINCT FROM OLD.evidence_id
     OR NEW.intake_terminal_id IS DISTINCT FROM OLD.intake_terminal_id
     OR NEW.selector_version IS DISTINCT FROM OLD.selector_version
     OR NEW.evidence_content_sha256
        IS DISTINCT FROM OLD.evidence_content_sha256
     OR NEW.route IS DISTINCT FROM OLD.route
     OR NEW.intake_reason_code IS DISTINCT FROM OLD.intake_reason_code
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'evidence extraction job identity is immutable'
      USING ERRCODE='23514';
  END IF;
  IF NEW.attempts<OLD.attempts THEN
    RAISE EXCEPTION 'evidence extraction attempts cannot decrease'
      USING ERRCODE='23514';
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
    (OLD.status='pending' AND NEW.status IN ('processing','skipped'))
    OR (
      OLD.status='processing'
      AND NEW.status IN (
        'processing','review_required','completed','skipped','error'
      )
    )
    OR (OLD.status='error' AND NEW.status IN ('processing','skipped'))
    OR (
      OLD.status='review_required'
      AND NEW.status IN ('completed','skipped')
    )
  ) THEN
    RAISE EXCEPTION 'invalid evidence extraction transition: % -> %',
      OLD.status,NEW.status
      USING ERRCODE='23514';
  END IF;
  NEW.updated_at=clock_timestamp();
  RETURN NEW;
END
$function$;

DO $role$
BEGIN
  IF to_regrole('memory_extraction_worker_maintainer') IS NOT NULL THEN
    DROP OWNED BY memory_extraction_worker_maintainer;
    DROP ROLE memory_extraction_worker_maintainer;
  END IF;
END
$role$;

COMMIT;
