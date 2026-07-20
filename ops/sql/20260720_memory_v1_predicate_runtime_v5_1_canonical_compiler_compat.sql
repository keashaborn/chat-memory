BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $migration$
DECLARE
  target regprocedure :=
    'memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  old_definition_sha constant text :=
    '693c5623530d32451198699360072dfc7475fa1bafcf134267a5528a38ac087f';
  new_definition_sha constant text :=
    'e79079a29210cf508dfc34571a9bb9501271d4ae3ba4990c1fb910f3f8877a06';
  canonical_compiler_sha constant text :=
    'af0e7b679480db10855cfb0ab2b705acd26a97238869e12b8b9a3f17bbc0024d';
  old_fragment constant text := $old$
     OR p_policy_compiler_sha256 <> encode(public.digest(convert_to(
       'memory_v1_relationship_policy_compiler_v14','UTF8'
     ),'sha256'),'hex')$old$;
  new_fragment constant text := $new$
     OR p_policy_compiler_sha256 <>
       'af0e7b679480db10855cfb0ab2b705acd26a97238869e12b8b9a3f17bbc0024d'$new$;
  definition text;
  definition_sha text;
BEGIN
  IF current_user<>'sage' OR target IS NULL THEN
    RAISE EXCEPTION 'V5.1 canonical compiler compatibility prerequisites are absent';
  END IF;
  SELECT pg_get_functiondef(target) INTO STRICT definition;
  definition_sha := encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');

  IF definition_sha=old_definition_sha THEN
    IF (length(definition)-length(replace(definition,old_fragment,'')))
         <>length(old_fragment)
       OR position(new_fragment IN definition)<>0 THEN
      RAISE EXCEPTION 'V5.1 persistence compiler guard is not uniquely replaceable';
    END IF;
    definition := replace(definition,old_fragment,new_fragment);
    EXECUTE definition;
  ELSIF definition_sha=new_definition_sha THEN
    NULL;
  ELSE
    RAISE EXCEPTION 'V5.1 persistence function definition drifted: %',
      definition_sha;
  END IF;

  SELECT pg_get_functiondef(target) INTO STRICT definition;
  definition_sha := encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');
  IF position(new_fragment IN definition)=0
     OR position(old_fragment IN definition)<>0
     OR position(canonical_compiler_sha IN definition)=0 THEN
    RAISE EXCEPTION 'V5.1 canonical compiler guard was not installed';
  END IF;
  RAISE NOTICE 'V5.1 canonical compiler function sha256=%',definition_sha;
END
$migration$;

ALTER FUNCTION memory.persist_owner_v5_1_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) OWNER TO memory_v5_local_inference_maintainer;

REVOKE ALL ON FUNCTION memory.persist_owner_v5_1_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;

GRANT EXECUTE ON FUNCTION memory.persist_owner_v5_1_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) TO brains_app;

CREATE OR REPLACE FUNCTION memory.requeue_owner_v5_1_persistence_mismatch_v1(
  p_operation_id uuid,
  p_job_id uuid,
  p_expected_content_sha256 text,
  p_failure_operation_id uuid,
  p_completion_event_id uuid
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
SET search_path='pg_catalog'
AS $function$
DECLARE
  actor uuid;
  current_job memory.evidence_extraction_job%ROWTYPE;
  failure memory.evidence_extraction_event%ROWTYPE;
  completion memory.v5_local_inference_event%ROWTYPE;
  replayed memory.evidence_extraction_event%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5.1 persistence recovery requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_job_id IS NULL
     OR p_failure_operation_id IS NULL OR p_completion_event_id IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'V5.1 persistence recovery inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_v5_1_persistence_recovery',actor::text,
      p_job_id::text),0
  ));
  SELECT event.* INTO replayed
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id=actor AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.job_id<>p_job_id OR replayed.event_type<>'error'
       OR replayed.from_status<>'error' OR replayed.to_status<>'error'
       OR replayed.actor_type<>'admin'
       OR replayed.actor_ref IS DISTINCT FROM
          'v5_1_persistence_contract_recovery'
       OR replayed.details->>'expected_content_sha256'
          IS DISTINCT FROM p_expected_content_sha256
       OR (replayed.details->>'failure_operation_id')::uuid
          IS DISTINCT FROM p_failure_operation_id
       OR (replayed.details->>'completion_event_id')::uuid
          IS DISTINCT FROM p_completion_event_id
       OR replayed.details->>'reason_code'
          IS DISTINCT FROM 'canonical_compiler_hash_compatibility' THEN
      RAISE EXCEPTION 'V5.1 persistence recovery replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT p_job_id,'error'::text,1,'replayed'::text;
    RETURN;
  END IF;

  SELECT job.* INTO current_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND OR current_job.status<>'error'
     OR current_job.route<>'relational_extraction'
     OR current_job.attempts<>1
     OR current_job.evidence_content_sha256<>p_expected_content_sha256
     OR current_job.lease_token IS NOT NULL
     OR current_job.lease_expires_at IS NOT NULL
     OR current_job.last_error IS DISTINCT FROM
        'local_inference_rejected: local_persistence_contract_mismatch' THEN
    RAISE EXCEPTION 'V5.1 persistence recovery job binding is invalid'
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
    RAISE EXCEPTION 'V5.1 persistence recovery evidence or packets changed'
      USING ERRCODE='23514';
  END IF;

  SELECT event.* INTO failure
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id=actor AND event.job_id=p_job_id
    AND event.operation_id=p_failure_operation_id;
  IF NOT FOUND OR failure.event_type<>'error'
     OR failure.from_status<>'processing' OR failure.to_status<>'error'
     OR failure.details->>'error_class'<>'local_inference_rejected'
     OR (failure.details->>'attempt')::integer<>1 THEN
    RAISE EXCEPTION 'V5.1 persistence recovery failure binding is invalid'
      USING ERRCODE='23514';
  END IF;

  SELECT event.* INTO completion
  FROM memory.v5_local_inference_event AS event
  WHERE event.owner_user_id=actor AND event.job_id=p_job_id
    AND event.event_id=p_completion_event_id;
  IF NOT FOUND OR completion.action<>'completed'
     OR completion.outcome<>'rejected'
     OR completion.rejection_code<>'local_persistence_contract_mismatch'
     OR completion.local_model_calls<>1 OR completion.external_model_calls<>0
     OR EXISTS (
       SELECT 1 FROM memory.evidence_extraction_packet_v5_local AS packet
       WHERE packet.owner_user_id=actor AND packet.job_id=p_job_id
     ) THEN
    RAISE EXCEPTION 'V5.1 persistence recovery completion binding is invalid'
      USING ERRCODE='23514';
  END IF;

  UPDATE memory.evidence_extraction_job AS job
  SET available_at=current_job.created_at
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id;
  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,from_status,to_status,
    actor_type,actor_ref,details
  ) VALUES (
    actor,p_job_id,p_operation_id,'error','error','error','admin',
    'v5_1_persistence_contract_recovery',jsonb_build_object(
      'expected_content_sha256',p_expected_content_sha256,
      'failure_operation_id',p_failure_operation_id,
      'completion_event_id',p_completion_event_id,
      'reason_code','canonical_compiler_hash_compatibility'
    )
  );
  RETURN QUERY SELECT p_job_id,'error'::text,1,'applied'::text;
END
$function$;

ALTER FUNCTION memory.requeue_owner_v5_1_persistence_mismatch_v1(
  uuid,uuid,text,uuid,uuid
) OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.requeue_owner_v5_1_persistence_mismatch_v1(
  uuid,uuid,text,uuid,uuid
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.requeue_owner_v5_1_persistence_mismatch_v1(
  uuid,uuid,text,uuid,uuid
) TO brains_app;

COMMIT;
