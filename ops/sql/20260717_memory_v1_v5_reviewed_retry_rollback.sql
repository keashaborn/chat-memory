BEGIN;

DROP FUNCTION IF EXISTS memory.requeue_owner_skipped_evidence_job_v5(
  uuid,uuid,text,uuid,uuid,text,integer,text
);

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
  IF NEW.checkpoint_sequence<OLD.checkpoint_sequence
     OR NEW.checkpoint_sequence>OLD.checkpoint_sequence+1 THEN
    RAISE EXCEPTION 'invalid evidence extraction checkpoint sequence'
      USING ERRCODE='23514';
  END IF;
  IF NEW.checkpoint_sequence=OLD.checkpoint_sequence
     AND NEW.checkpoint_sha256 IS DISTINCT FROM OLD.checkpoint_sha256 THEN
    RAISE EXCEPTION 'evidence extraction checkpoint hash is immutable'
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

DO $role_cleanup$
BEGIN
  IF to_regrole('memory_extraction_retry_maintainer') IS NOT NULL THEN
    DROP OWNED BY memory_extraction_retry_maintainer;
    DROP ROLE memory_extraction_retry_maintainer;
  END IF;
END
$role_cleanup$;

COMMIT;
