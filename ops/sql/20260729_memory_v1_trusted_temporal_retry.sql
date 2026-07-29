\set ON_ERROR_STOP on

BEGIN;

DO $preflight$
BEGIN
  IF session_user <> 'sage'
     OR to_regrole('memory_v5_local_inference_maintainer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.v5_local_inference_event') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5') IS NULL THEN
    RAISE EXCEPTION 'trusted temporal retry prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION
memory.requeue_owner_trusted_temporal_failure_v1(
  p_operation_id uuid,
  p_job_id uuid,
  p_expected_content_sha256 text,
  p_prior_failure_operation_id uuid,
  p_local_completion_event_id uuid,
  p_expected_attempts integer,
  p_prior_policy_compiler_sha256 text,
  p_retry_contract_version text
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
  prior_failure memory.evidence_extraction_event%ROWTYPE;
  completion memory.v5_local_inference_event%ROWTYPE;
  replayed memory.evidence_extraction_event%ROWTYPE;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'trusted temporal retry requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_job_id IS NULL
     OR p_prior_failure_operation_id IS NULL
     OR p_local_completion_event_id IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_prior_policy_compiler_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_attempts NOT BETWEEN 1 AND 19
     OR p_retry_contract_version <>
        'memory_v1_trusted_temporal_boundary_compiler_repair_v1' THEN
    RAISE EXCEPTION 'trusted temporal retry inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws(
      '|',
      'memory_v1_trusted_temporal_retry',
      actor::text,
      p_job_id::text
    ),
    0
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
       OR replayed.actor_ref IS DISTINCT FROM
          'trusted_temporal_compiler_retry'
       OR replayed.details->>'expected_content_sha256'
          IS DISTINCT FROM p_expected_content_sha256
       OR (replayed.details->>'prior_failure_operation_id')::uuid
          IS DISTINCT FROM p_prior_failure_operation_id
       OR (replayed.details->>'local_completion_event_id')::uuid
          IS DISTINCT FROM p_local_completion_event_id
       OR (replayed.details->>'attempts')::integer
          IS DISTINCT FROM p_expected_attempts
       OR replayed.details->>'prior_policy_compiler_sha256'
          IS DISTINCT FROM p_prior_policy_compiler_sha256
       OR replayed.details->>'retry_contract_version'
          IS DISTINCT FROM p_retry_contract_version
       OR replayed.details->>'rejection_code'
          IS DISTINCT FROM
          'trusted_source_time_asserted_by_provider' THEN
      RAISE EXCEPTION 'trusted temporal retry replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      p_job_id,
      'pending'::text,
      p_expected_attempts,
      'replayed'::text;
    RETURN;
  END IF;

  SELECT job.* INTO current_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor
    AND job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND
     OR current_job.status<>'skipped'
     OR current_job.route<>'relational_extraction'
     OR current_job.attempts<>p_expected_attempts
     OR current_job.evidence_content_sha256<>
        p_expected_content_sha256
     OR current_job.lease_token IS NOT NULL
     OR current_job.lease_expires_at IS NOT NULL
     OR current_job.last_error IS DISTINCT FROM
        'local_inference_rejected: '||
        'trusted_source_time_asserted_by_provider' THEN
    RAISE EXCEPTION 'trusted temporal retry job binding is invalid'
      USING ERRCODE='23514';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id=actor
      AND evidence.evidence_id=current_job.evidence_id
      AND evidence.content_sha256=p_expected_content_sha256
      AND evidence.status='active'
  ) OR EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_packet_v5_local AS packet
    WHERE packet.owner_user_id=actor
      AND packet.job_id=p_job_id
  ) OR EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_packet_v5 AS packet
    WHERE packet.owner_user_id=actor
      AND packet.job_id=p_job_id
  ) THEN
    RAISE EXCEPTION 'trusted temporal retry evidence or packets changed'
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
     OR prior_failure.details->>'error_class'<>
        'local_inference_rejected'
     OR (prior_failure.details->>'attempt')::integer<>
        p_expected_attempts THEN
    RAISE EXCEPTION 'trusted temporal retry failure binding is invalid'
      USING ERRCODE='23514';
  END IF;

  SELECT event.* INTO completion
  FROM memory.v5_local_inference_event AS event
  WHERE event.owner_user_id=actor
    AND event.job_id=p_job_id
    AND event.event_id=p_local_completion_event_id;
  IF NOT FOUND
     OR completion.action<>'completed'
     OR completion.outcome<>'rejected'
     OR completion.rejection_code<>
        'trusted_source_time_asserted_by_provider'
     OR completion.target_content_sha256<>
        p_expected_content_sha256
     OR completion.policy_compiler_sha256<>
        p_prior_policy_compiler_sha256
     OR completion.local_model_calls<>1
     OR completion.external_model_calls<>0
     OR NOT EXISTS (
       SELECT 1
       FROM memory.v5_local_inference_event AS reservation
       WHERE reservation.owner_user_id=actor
         AND reservation.event_id=completion.reservation_event_id
         AND reservation.job_id=p_job_id
         AND reservation.run_id=completion.run_id
         AND reservation.action='reserved'
         AND reservation.target_content_sha256=
            p_expected_content_sha256
         AND reservation.policy_compiler_sha256=
            p_prior_policy_compiler_sha256
     ) THEN
    RAISE EXCEPTION 'trusted temporal retry ledger binding is invalid'
      USING ERRCODE='23514';
  END IF;

  UPDATE memory.evidence_extraction_job AS job
  SET
    status='pending',
    available_at=clock_timestamp(),
    worker_id=NULL,
    last_error=NULL
  WHERE job.owner_user_id=actor
    AND job.job_id=p_job_id;

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,
    job_id,
    operation_id,
    event_type,
    from_status,
    to_status,
    actor_type,
    actor_ref,
    details
  ) VALUES (
    actor,
    p_job_id,
    p_operation_id,
    'queued',
    'skipped',
    'pending',
    'admin',
    'trusted_temporal_compiler_retry',
    jsonb_build_object(
      'expected_content_sha256',
      p_expected_content_sha256,
      'prior_failure_operation_id',
      p_prior_failure_operation_id,
      'local_completion_event_id',
      p_local_completion_event_id,
      'attempts',
      p_expected_attempts,
      'rejection_code',
      'trusted_source_time_asserted_by_provider',
      'prior_policy_compiler_sha256',
      p_prior_policy_compiler_sha256,
      'retry_contract_version',
      p_retry_contract_version
    )
  );

  RETURN QUERY SELECT
    p_job_id,
    'pending'::text,
    p_expected_attempts,
    'applied'::text;
END
$function$;

ALTER FUNCTION memory.requeue_owner_trusted_temporal_failure_v1(
  uuid,uuid,text,uuid,uuid,integer,text,text
) OWNER TO memory_v5_local_inference_maintainer;

REVOKE ALL ON FUNCTION
memory.requeue_owner_trusted_temporal_failure_v1(
  uuid,uuid,text,uuid,uuid,integer,text,text
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;

GRANT EXECUTE ON FUNCTION
memory.requeue_owner_trusted_temporal_failure_v1(
  uuid,uuid,text,uuid,uuid,integer,text,text
) TO brains_app;

COMMIT;
