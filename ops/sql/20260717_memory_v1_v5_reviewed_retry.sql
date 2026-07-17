BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage'
     OR to_regrole('brains_app') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.current_project_thread_binding_v5') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL THEN
    RAISE EXCEPTION 'reviewed extraction retry prerequisites are absent';
  END IF;
  IF to_regrole('memory_extraction_retry_maintainer') IS NULL THEN
    CREATE ROLE memory_extraction_retry_maintainer NOLOGIN NOINHERIT;
  END IF;
END
$preflight$;

GRANT USAGE ON SCHEMA memory TO memory_extraction_retry_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_extraction_retry_maintainer;
GRANT SELECT,UPDATE ON memory.evidence_extraction_job
  TO memory_extraction_retry_maintainer;
GRANT SELECT,INSERT ON memory.evidence_extraction_event
  TO memory_extraction_retry_maintainer;
GRANT SELECT ON
  memory.evidence,
  memory.project_thread_binding_event,
  memory.current_project_thread_binding_v5,
  memory.evidence_extraction_packet_v5
TO memory_extraction_retry_maintainer;

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
    OR (OLD.status='skipped' AND NEW.status='pending')
  ) THEN
    RAISE EXCEPTION 'invalid evidence extraction transition: % -> %',
      OLD.status,NEW.status
      USING ERRCODE='23514';
  END IF;
  NEW.updated_at=clock_timestamp();
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.requeue_owner_skipped_evidence_job_v5(
  p_operation_id uuid,
  p_job_id uuid,
  p_expected_content_sha256 text,
  p_project_binding_event_id uuid,
  p_prior_failure_operation_id uuid,
  p_expected_error_class text,
  p_expected_attempts integer,
  p_reason_code text
)
RETURNS TABLE(
  job_id uuid,
  status text,
  attempts integer,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  current_job memory.evidence_extraction_job%ROWTYPE;
  evidence_record memory.evidence%ROWTYPE;
  prior_failure memory.evidence_extraction_event%ROWTYPE;
  replayed memory.evidence_extraction_event%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'reviewed extraction retry requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_job_id IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_project_binding_event_id IS NULL
     OR p_prior_failure_operation_id IS NULL
     OR p_expected_error_class IS NULL
     OR p_expected_error_class !~ '^[a-z][a-z0-9_]{2,99}$'
     OR p_expected_attempts NOT BETWEEN 1 AND 19
     OR p_reason_code<>'diagnostic_observability_upgrade' THEN
    RAISE EXCEPTION 'reviewed extraction retry inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||':reviewed_extraction_retry:'||p_job_id::text,0
  ));

  SELECT event.* INTO replayed
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.job_id<>p_job_id
       OR replayed.event_type<>'queued'
       OR replayed.from_status<>'skipped'
       OR replayed.to_status<>'pending'
       OR replayed.actor_type<>'admin'
       OR replayed.actor_ref IS DISTINCT FROM 'reviewed_retry'
       OR replayed.details->>'expected_content_sha256'
          IS DISTINCT FROM p_expected_content_sha256
       OR (replayed.details->>'project_binding_event_id')::uuid
          IS DISTINCT FROM p_project_binding_event_id
       OR (replayed.details->>'prior_failure_operation_id')::uuid
          IS DISTINCT FROM p_prior_failure_operation_id
       OR replayed.details->>'expected_error_class'
          IS DISTINCT FROM p_expected_error_class
       OR (replayed.details->>'attempts')::integer
          IS DISTINCT FROM p_expected_attempts
       OR replayed.details->>'reason_code' IS DISTINCT FROM p_reason_code THEN
      RAISE EXCEPTION 'reviewed extraction retry replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      p_job_id,replayed.to_status::text,p_expected_attempts,'replayed'::text;
    RETURN;
  END IF;

  SELECT job.* INTO current_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND
     OR current_job.status<>'skipped'
     OR current_job.route<>'relational_extraction'
     OR current_job.attempts<>p_expected_attempts
     OR current_job.evidence_content_sha256<>p_expected_content_sha256
     OR current_job.lease_token IS NOT NULL
     OR current_job.lease_expires_at IS NOT NULL
     OR split_part(coalesce(current_job.last_error,''),':',1)
        <>p_expected_error_class THEN
    RAISE EXCEPTION 'reviewed skipped extraction job does not match'
      USING ERRCODE='23514';
  END IF;

  SELECT event.* INTO prior_failure
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id=actor
    AND event.job_id=p_job_id
    AND event.operation_id=p_prior_failure_operation_id;
  IF NOT FOUND
     OR prior_failure.event_type<>'skipped'
     OR prior_failure.from_status<>'processing'
     OR prior_failure.to_status<>'skipped'
     OR prior_failure.details->>'error_class'<>p_expected_error_class
     OR (prior_failure.details->>'attempt')::integer<>p_expected_attempts THEN
    RAISE EXCEPTION 'reviewed retry prior failure binding is invalid'
      USING ERRCODE='23514';
  END IF;

  SELECT evidence.* INTO evidence_record
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=current_job.evidence_id
    AND evidence.content_sha256=p_expected_content_sha256;
  IF NOT FOUND
     OR evidence_record.metadata->>'thread_id' IS NULL
     OR EXISTS (
       SELECT 1
       FROM memory.evidence_extraction_packet_v5 AS packet
       WHERE packet.owner_user_id=actor AND packet.job_id=p_job_id
     )
     OR NOT EXISTS (
       SELECT 1
       FROM memory.current_project_thread_binding_v5 AS binding
       WHERE binding.owner_user_id=actor
         AND binding.binding_event_id=p_project_binding_event_id
         AND binding.thread_id::text=evidence_record.metadata->>'thread_id'
     ) THEN
    RAISE EXCEPTION 'reviewed retry evidence or project binding changed'
      USING ERRCODE='23514';
  END IF;

  UPDATE memory.evidence_extraction_job AS job
  SET
    status='pending',
    available_at=clock_timestamp(),
    worker_id=NULL,
    last_error=NULL
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id;

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,from_status,to_status,
    actor_type,actor_ref,details
  ) VALUES (
    actor,p_job_id,p_operation_id,'queued','skipped','pending',
    'admin','reviewed_retry',jsonb_build_object(
      'expected_content_sha256',p_expected_content_sha256,
      'project_binding_event_id',p_project_binding_event_id,
      'prior_failure_operation_id',p_prior_failure_operation_id,
      'expected_error_class',p_expected_error_class,
      'attempts',p_expected_attempts,
      'reason_code',p_reason_code
    )
  );

  RETURN QUERY SELECT
    p_job_id,'pending'::text,p_expected_attempts,'applied'::text;
END
$function$;

ALTER FUNCTION memory.requeue_owner_skipped_evidence_job_v5(
  uuid,uuid,text,uuid,uuid,text,integer,text
) OWNER TO memory_extraction_retry_maintainer;

REVOKE ALL ON FUNCTION memory.requeue_owner_skipped_evidence_job_v5(
  uuid,uuid,text,uuid,uuid,text,integer,text
) FROM PUBLIC,brains_app,memory_extraction_retry_maintainer;
GRANT EXECUTE ON FUNCTION memory.requeue_owner_skipped_evidence_job_v5(
  uuid,uuid,text,uuid,uuid,text,integer,text
) TO brains_app;

COMMIT;
