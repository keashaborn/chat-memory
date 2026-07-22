BEGIN;

DO $migration$
DECLARE
  persistence_oid constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  claim_oid constant regprocedure :=
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure;
  persistence_before constant text :=
    'a8a8e97bba56577a1f7bf0fbee932b1094b4e6c50200891eedb7c1970bcec3fd';
  persistence_after constant text :=
    '0ca8e0240ce8d4d9c1d43f3457c4e2b2a6475176c47a240059f644125af41f49';
  claim_before constant text :=
    '01052eb7ea5a2add2a9795bcc960408a7702fdc1baee83e17b12722889307970';
  claim_after constant text :=
    'cea8bc6ee880a4897b975191a80ff35a36fb22751c1bb2bd601e7836f620cd29';
  persistence_old constant text :=
$old$OR p_policy_compiler_sha256 NOT IN (
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v1','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v2','UTF8'
       ),'sha256'),'hex')
     )$old$;
  persistence_new constant text :=
$new$OR p_policy_compiler_sha256 NOT IN (
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v1','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v2','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v3','UTF8'
       ),'sha256'),'hex')
     )$new$;
  claim_old constant text := '     OR p_max_attempts NOT BETWEEN 1 AND 3';
  claim_new constant text := '     OR p_max_attempts NOT BETWEEN 1 AND 4';
  source text;
  source_sha text;
BEGIN
  SELECT pg_get_functiondef(persistence_oid) INTO source;
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>persistence_before
     OR (length(source)-length(replace(source,persistence_old,'')))
          / length(persistence_old)<>1 THEN
    RAISE EXCEPTION 'V5.2 persistence compiler baseline changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  source:=replace(source,persistence_old,persistence_new);
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>persistence_after THEN
    RAISE EXCEPTION 'V5.2 persistence compiler replacement changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  EXECUTE source;

  SELECT pg_get_functiondef(claim_oid) INTO source;
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>claim_before
     OR (length(source)-length(replace(source,claim_old,'')))
          / length(claim_old)<>1 THEN
    RAISE EXCEPTION 'V5.2 claim ceiling baseline changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  source:=replace(source,claim_old,claim_new);
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>claim_after THEN
    RAISE EXCEPTION 'V5.2 claim ceiling replacement changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  EXECUTE source;
END
$migration$;

ALTER FUNCTION memory.persist_owner_v5_2_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.persist_owner_v5_2_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.persist_owner_v5_2_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) TO brains_app;

ALTER FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) TO brains_app;

CREATE OR REPLACE FUNCTION memory.requeue_owner_v5_2_persistence_mismatch_v1(
  p_operation_id uuid,
  p_job_id uuid,
  p_expected_content_sha256 text,
  p_failure_operation_id uuid,
  p_completion_event_id uuid,
  p_expected_attempts integer,
  p_reason_code text
)
RETURNS TABLE(job_id uuid,status text,attempts integer,apply_outcome text)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  current_job memory.evidence_extraction_job%ROWTYPE;
  failure memory.evidence_extraction_event%ROWTYPE;
  completion memory.v5_local_inference_event%ROWTYPE;
  replayed memory.evidence_extraction_event%ROWTYPE;
  rejection_code constant text := 'local_persistence_contract_mismatch';
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5.2 persistence retry requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_job_id IS NULL
     OR p_failure_operation_id IS NULL OR p_completion_event_id IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_attempts<>3
     OR p_reason_code<>'semantic_compiler_v3_persistence_compatibility' THEN
    RAISE EXCEPTION 'V5.2 persistence retry inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_2_persistence_retry',actor::text,p_job_id::text),0));

  SELECT event.* INTO replayed
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id=actor AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.job_id<>p_job_id OR replayed.event_type<>'queued'
       OR replayed.from_status<>'skipped' OR replayed.to_status<>'pending'
       OR replayed.actor_type<>'admin'
       OR replayed.actor_ref IS DISTINCT FROM 'v5_2_persistence_retry'
       OR replayed.details->>'expected_content_sha256'
          IS DISTINCT FROM p_expected_content_sha256
       OR (replayed.details->>'failure_operation_id')::uuid
          IS DISTINCT FROM p_failure_operation_id
       OR (replayed.details->>'completion_event_id')::uuid
          IS DISTINCT FROM p_completion_event_id
       OR (replayed.details->>'attempts')::integer<>p_expected_attempts
       OR replayed.details->>'rejection_code'<>rejection_code
       OR replayed.details->>'reason_code'<>p_reason_code THEN
      RAISE EXCEPTION 'V5.2 persistence retry replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT p_job_id,'pending'::text,p_expected_attempts,
      'replayed'::text;
    RETURN;
  END IF;

  SELECT job.* INTO current_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id FOR UPDATE;
  IF NOT FOUND OR current_job.status<>'skipped'
     OR current_job.route<>'relational_extraction'
     OR current_job.attempts<>p_expected_attempts
     OR current_job.evidence_content_sha256<>p_expected_content_sha256
     OR current_job.lease_token IS NOT NULL
     OR current_job.lease_expires_at IS NOT NULL
     OR current_job.last_error IS DISTINCT FROM
        'local_inference_rejected: '||rejection_code THEN
    RAISE EXCEPTION 'V5.2 persistence retry job binding is invalid'
      USING ERRCODE='23514';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id=actor
      AND evidence.evidence_id=current_job.evidence_id
      AND evidence.content_sha256=p_expected_content_sha256
      AND evidence.status='active'
  ) OR EXISTS (
    SELECT 1 FROM memory.evidence_extraction_packet_v5_local AS packet
    WHERE packet.owner_user_id=actor AND packet.job_id=p_job_id
  ) OR EXISTS (
    SELECT 1 FROM memory.evidence_extraction_packet_v5 AS packet
    WHERE packet.owner_user_id=actor AND packet.job_id=p_job_id
  ) THEN
    RAISE EXCEPTION 'V5.2 persistence retry evidence or packets changed'
      USING ERRCODE='23514';
  END IF;

  SELECT event.* INTO failure
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id=actor AND event.job_id=p_job_id
    AND event.operation_id=p_failure_operation_id;
  IF NOT FOUND OR failure.event_type<>'skipped'
     OR failure.from_status<>'processing' OR failure.to_status<>'skipped'
     OR failure.details->>'error_class'<>'local_inference_rejected'
     OR (failure.details->>'attempt')::integer<>p_expected_attempts THEN
    RAISE EXCEPTION 'V5.2 persistence retry failure binding is invalid'
      USING ERRCODE='23514';
  END IF;
  SELECT event.* INTO completion
  FROM memory.v5_local_inference_event AS event
  WHERE event.owner_user_id=actor AND event.job_id=p_job_id
    AND event.event_id=p_completion_event_id;
  IF NOT FOUND OR completion.action<>'completed'
     OR completion.outcome<>'rejected'
     OR completion.rejection_code<>rejection_code
     OR completion.local_model_calls<>1 OR completion.external_model_calls<>0
     OR NOT EXISTS (
       SELECT 1 FROM memory.v5_local_inference_event AS reservation
       WHERE reservation.owner_user_id=actor
         AND reservation.event_id=completion.reservation_event_id
         AND reservation.job_id=p_job_id AND reservation.action='reserved'
     ) THEN
    RAISE EXCEPTION 'V5.2 persistence retry completion binding is invalid'
      USING ERRCODE='23514';
  END IF;

  UPDATE memory.evidence_extraction_job AS job
  SET status='pending',available_at=clock_timestamp(),worker_id=NULL,
    last_error=NULL
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id;
  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,from_status,to_status,
    actor_type,actor_ref,details
  ) VALUES (
    actor,p_job_id,p_operation_id,'queued','skipped','pending','admin',
    'v5_2_persistence_retry',jsonb_build_object(
      'expected_content_sha256',p_expected_content_sha256,
      'failure_operation_id',p_failure_operation_id,
      'completion_event_id',p_completion_event_id,
      'attempts',p_expected_attempts,'rejection_code',rejection_code,
      'reason_code',p_reason_code
    )
  );
  RETURN QUERY SELECT p_job_id,'pending'::text,p_expected_attempts,
    'applied'::text;
END
$function$;

ALTER FUNCTION memory.requeue_owner_v5_2_persistence_mismatch_v1(
  uuid,uuid,text,uuid,uuid,integer,text
) OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.requeue_owner_v5_2_persistence_mismatch_v1(
  uuid,uuid,text,uuid,uuid,integer,text
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.requeue_owner_v5_2_persistence_mismatch_v1(
  uuid,uuid,text,uuid,uuid,integer,text
) TO brains_app;

COMMIT;
