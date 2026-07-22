BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 local orphan recovery migration requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regrole('memory_v5_local_inference_maintainer') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.v5_local_inference_event') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure(
       'memory.complete_owner_v5_local_inference_v1(uuid,uuid,uuid,uuid,text,integer,text,text,text,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5 local orphan recovery prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.recover_owner_v5_local_orphan_v1(
  p_operation_id uuid,
  p_completion_operation_id uuid,
  p_job_id uuid,
  p_reservation_event_id uuid,
  p_expected_content_sha256 text
)
RETURNS TABLE(
  job_id uuid,
  status text,
  recovery_event_id uuid,
  completion_event_id uuid,
  accounted_local_model_calls integer,
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
  reservation memory.v5_local_inference_event%ROWTYPE;
  replayed memory.evidence_extraction_event%ROWTYPE;
  completion memory.v5_local_inference_event%ROWTYPE;
  new_recovery_event_id uuid := gen_random_uuid();
  new_completion_event_id uuid := gen_random_uuid();
  next_result jsonb;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5 local orphan recovery requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_completion_operation_id IS NULL
     OR p_operation_id=p_completion_operation_id OR p_job_id IS NULL
     OR p_reservation_event_id IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'V5 local orphan recovery inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT event.* INTO replayed
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    SELECT local_event.* INTO completion
    FROM memory.v5_local_inference_event AS local_event
    WHERE local_event.owner_user_id=actor
      AND local_event.operation_id=p_completion_operation_id;
    IF replayed.job_id<>p_job_id
       OR replayed.event_type<>'skipped'
       OR replayed.from_status<>'processing'
       OR replayed.to_status<>'skipped'
       OR replayed.actor_ref<>'memory_v1_v5_local_orphan_recovery_v1'
       OR replayed.details->>'reservation_event_id'
          IS DISTINCT FROM p_reservation_event_id::text
       OR replayed.details->>'expected_content_sha256'
          IS DISTINCT FROM p_expected_content_sha256
       OR NOT FOUND
       OR completion.action<>'completed'
       OR completion.reservation_event_id<>p_reservation_event_id
       OR completion.job_id<>p_job_id
       OR completion.outcome<>'rejected'
       OR completion.rejection_code<>'local_worker_abandoned'
       OR completion.local_model_calls<>1
       OR completion.external_model_calls<>0 THEN
      RAISE EXCEPTION 'V5 local orphan recovery replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT p_job_id,'skipped'::text,replayed.event_id,
      completion.event_id,1,'replayed'::text;
    RETURN;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_v5_local_orphan_recovery',actor::text,
      p_job_id::text),0
  ));
  SELECT job.* INTO current_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND
     OR current_job.route<>'relational_extraction'
     OR current_job.status<>'processing'
     OR current_job.evidence_content_sha256<>p_expected_content_sha256
     OR current_job.attempts<1
     OR current_job.lease_token IS NULL
     OR current_job.lease_expires_at IS NULL
     OR current_job.lease_expires_at>clock_timestamp()-interval '60 seconds'
     OR current_job.worker_id IS NULL
     OR current_job.worker_id NOT LIKE
       'memory_v1_v5_local_inference_scheduler_v2:%' THEN
    RAISE EXCEPTION 'V5 local orphan target is absent or ineligible'
      USING ERRCODE='23514';
  END IF;

  SELECT local_event.* INTO reservation
  FROM memory.v5_local_inference_event AS local_event
  WHERE local_event.owner_user_id=actor
    AND local_event.event_id=p_reservation_event_id
    AND local_event.job_id=p_job_id
    AND local_event.action='reserved'
    AND local_event.outcome='reserved';
  IF NOT FOUND
     OR reservation.local_model_calls<>0
     OR reservation.external_model_calls<>0
     OR reservation.created_at>current_job.lease_expires_at
     OR EXISTS (
       SELECT 1
       FROM memory.v5_local_inference_event AS completed
       WHERE completed.owner_user_id=actor
         AND completed.reservation_event_id=p_reservation_event_id
         AND completed.action='completed'
     )
     OR EXISTS (
       SELECT 1
       FROM memory.v5_local_inference_event AS later_reservation
       WHERE later_reservation.owner_user_id=actor
         AND later_reservation.job_id=p_job_id
         AND later_reservation.action='reserved'
         AND (later_reservation.created_at,later_reservation.event_id)>
             (reservation.created_at,reservation.event_id)
     )
     OR EXISTS (
       SELECT 1
       FROM memory.evidence_extraction_packet_v5_local AS packet
       WHERE packet.owner_user_id=actor AND packet.job_id=p_job_id
     )
     OR EXISTS (
       SELECT 1
       FROM memory.evidence_extraction_packet_v5 AS packet
       WHERE packet.owner_user_id=actor AND packet.job_id=p_job_id
     ) THEN
    RAISE EXCEPTION 'V5 local orphan reservation is unsafe to recover'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_local_inference_event(
    event_id,owner_user_id,operation_id,run_id,job_id,
    target_job_sha256,target_content_sha256,reservation_event_id,
    action,outcome,provider_id,provider_version,
    provider_model_sha256,model_file_sha256,runtime_revision_sha256,
    policy_compiler_sha256,worker_id_sha256,local_model_calls,
    external_model_calls,rejection_code,provider_output_sha256,
    validator_packet_sha256,packet_storage_sha256,rolling_window_seconds,
    max_reserved_jobs,failure_threshold,reserved_jobs_in_window,
    consecutive_rejections,created_at
  ) VALUES (
    new_completion_event_id,actor,p_completion_operation_id,
    reservation.run_id,p_job_id,reservation.target_job_sha256,
    reservation.target_content_sha256,p_reservation_event_id,
    'completed','rejected',reservation.provider_id,
    reservation.provider_version,reservation.provider_model_sha256,
    reservation.model_file_sha256,reservation.runtime_revision_sha256,
    reservation.policy_compiler_sha256,reservation.worker_id_sha256,
    1,0,'local_worker_abandoned',NULL,NULL,NULL,
    reservation.rolling_window_seconds,reservation.max_reserved_jobs,
    reservation.failure_threshold,reservation.reserved_jobs_in_window,
    least(reservation.consecutive_rejections+1,10),
    reservation.created_at+interval '1 microsecond'
  );

  next_result := jsonb_set(
    current_job.result,
    '{final}',
    jsonb_build_object(
      'status','skipped',
      'reason_code','local_worker_abandoned',
      'source_prose_copied',false,
      'local_model_calls_accounted',1,
      'external_model_calls',0,
      'claims',0,
      'qdrant',0,
      'prompt_influence',0
    ),
    true
  );
  IF pg_column_size(next_result)>32768 THEN
    RAISE EXCEPTION 'V5 local orphan summary exceeds queue budget'
      USING ERRCODE='22023';
  END IF;

  UPDATE memory.evidence_extraction_job AS job
  SET status='skipped',lease_token=NULL,lease_expires_at=NULL,
      worker_id=NULL,last_error='local_worker_abandoned',result=next_result
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id;

  INSERT INTO memory.evidence_extraction_event(
    event_id,owner_user_id,job_id,operation_id,event_type,
    from_status,to_status,actor_type,actor_ref,details
  ) VALUES (
    new_recovery_event_id,actor,p_job_id,p_operation_id,'skipped',
    'processing','skipped','system',
    'memory_v1_v5_local_orphan_recovery_v1',
    jsonb_build_object(
      'reservation_event_id',p_reservation_event_id,
      'completion_event_id',new_completion_event_id,
      'expected_content_sha256',p_expected_content_sha256,
      'reason_code','local_worker_abandoned',
      'source_prose_copied',false,
      'local_model_calls_accounted',1,
      'external_model_calls',0,
      'claims',0,
      'qdrant',0,
      'prompt_influence',0
    )
  );

  RETURN QUERY SELECT p_job_id,'skipped'::text,new_recovery_event_id,
    new_completion_event_id,1,'applied'::text;
END
$function$;

ALTER FUNCTION memory.recover_owner_v5_local_orphan_v1(
  uuid,uuid,uuid,uuid,text
) OWNER TO memory_v5_local_inference_maintainer;

REVOKE ALL ON FUNCTION memory.recover_owner_v5_local_orphan_v1(
  uuid,uuid,uuid,uuid,text
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.recover_owner_v5_local_orphan_v1(
  uuid,uuid,uuid,uuid,text
) TO brains_app;

COMMIT;
