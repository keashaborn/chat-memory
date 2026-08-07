CREATE OR REPLACE FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(p_operation_id uuid, p_run_id uuid, p_route text, p_worker_id text, p_lease_seconds integer, p_max_attempts integer, p_provider_id text, p_provider_version text, p_provider_model_sha256 text, p_rolling_window_seconds integer, p_max_reserved_calls integer, p_failure_threshold integer)
RETURNS TABLE(
  job_id uuid,
  evidence_id uuid,
  lease_token uuid,
  status text,
  route text,
  attempts integer,
  evidence_content_sha256 text,
  evidence_kind text,
  evidence_source_system text,
  evidence_external_id text,
  evidence_content text,
  evidence_observed_at timestamptz,
  evidence_recorded_at timestamptz,
  evidence_sensitivity text,
  checkpoint_sequence integer,
  checkpoint_sha256 text,
  result jsonb,
  reservation_event_id uuid,
  control_outcome text,
  reserved_calls_in_window integer,
  consecutive_rejections integer,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  worker_sha text;
  new_lease_token uuid := gen_random_uuid();
  new_reservation_id uuid := gen_random_uuid();
  replayed memory.v5_extraction_call_event%ROWTYPE;
  claimed record;
  reserved_count integer;
  latest_completion_count integer;
  latest_all_rejected boolean;
  rejection_count integer;
  block_reason text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'bounded V5 extraction claim requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_run_id IS NULL
     OR p_route<>'relational_extraction'
     OR p_worker_id IS NULL
     OR btrim(p_worker_id)=''
     OR length(p_worker_id)>500
     OR p_lease_seconds NOT BETWEEN 30 AND 3600
     OR p_max_attempts NOT BETWEEN 1 AND 3
     OR p_provider_id<>'openai_responses'
     OR p_provider_version IS NULL
     OR p_provider_version !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
     OR p_provider_model_sha256 !~ '^[0-9a-f]{64}$'
     OR p_rolling_window_seconds NOT BETWEEN 3600 AND 604800
     OR p_max_reserved_calls NOT BETWEEN 1 AND 100
     OR p_failure_threshold NOT BETWEEN 1 AND 10 THEN
    RAISE EXCEPTION 'bounded V5 extraction claim inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  worker_sha := encode(
    public.digest(convert_to(p_worker_id,'UTF8'),'sha256'),'hex'
  );
  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_v5_extraction',actor::text,p_route),0
  ));

  SELECT event.* INTO replayed
  FROM memory.v5_extraction_call_event AS event
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.run_id<>p_run_id
       OR replayed.action NOT IN ('blocked','reserved')
       OR replayed.provider_id<>p_provider_id
       OR replayed.provider_version<>p_provider_version
       OR replayed.provider_model_sha256<>p_provider_model_sha256
       OR replayed.worker_id_sha256<>worker_sha
       OR replayed.rolling_window_seconds<>p_rolling_window_seconds
       OR replayed.max_reserved_calls<>p_max_reserved_calls
       OR replayed.failure_threshold<>p_failure_threshold THEN
      RAISE EXCEPTION 'bounded V5 extraction claim replay conflicts'
        USING ERRCODE='23514';
    END IF;
    IF replayed.action='blocked' THEN
      RETURN QUERY SELECT
        NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,p_route,NULL::integer,
        NULL::text,NULL::text,NULL::text,NULL::text,NULL::text,
        NULL::timestamptz,NULL::timestamptz,NULL::text,NULL::integer,
        NULL::text,'{}'::jsonb,NULL::uuid,replayed.outcome,
        replayed.reserved_calls_in_window,replayed.consecutive_rejections,
        'replayed'::text;
      RETURN;
    END IF;
    RETURN QUERY
    SELECT
      extraction_job.job_id,
      extraction_job.evidence_id,
      (claim_event.details->>'lease_token')::uuid,
      extraction_job.status::text,
      extraction_job.route,
      extraction_job.attempts,
      extraction_job.evidence_content_sha256,
      evidence.kind::text,
      evidence.source_system,
      evidence.external_id,
      evidence.content,
      evidence.observed_at,
      evidence.recorded_at,
      evidence.sensitivity::text,
      extraction_job.checkpoint_sequence,
      extraction_job.checkpoint_sha256,
      extraction_job.result,
      replayed.event_id,
      replayed.outcome,
      replayed.reserved_calls_in_window,
      replayed.consecutive_rejections,
      'replayed'::text
    FROM memory.evidence_extraction_job AS extraction_job
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=extraction_job.owner_user_id
     AND evidence.evidence_id=extraction_job.evidence_id
    JOIN memory.evidence_extraction_event AS claim_event
      ON claim_event.owner_user_id=extraction_job.owner_user_id
     AND claim_event.job_id=extraction_job.job_id
     AND claim_event.event_type='claimed'
     AND claim_event.operation_id=p_operation_id
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.job_id=replayed.job_id;
    RETURN;
  END IF;

  SELECT count(*)::integer INTO reserved_count
  FROM memory.v5_extraction_call_event AS event
  WHERE event.owner_user_id=actor
    AND event.action='reserved'
    AND event.created_at>=
      clock_timestamp()-make_interval(secs=>p_rolling_window_seconds);

  SELECT
    count(*)::integer,
    coalesce(bool_and(recent.outcome='rejected'),false)
  INTO latest_completion_count,latest_all_rejected
  FROM (
    SELECT event.outcome
    FROM memory.v5_extraction_call_event AS event
    WHERE event.owner_user_id=actor
      AND event.action='completed'
    ORDER BY event.created_at DESC,event.event_id DESC
    LIMIT p_failure_threshold
  ) AS recent;
  rejection_count := CASE
    WHEN latest_completion_count=p_failure_threshold
     AND latest_all_rejected THEN p_failure_threshold
    ELSE 0
  END;

  IF reserved_count>=p_max_reserved_calls THEN
    block_reason := 'quota_exhausted';
  ELSIF rejection_count>=p_failure_threshold THEN
    block_reason := 'circuit_open';
  END IF;
  IF block_reason IS NOT NULL THEN
    INSERT INTO memory.v5_extraction_call_event(
      event_id,owner_user_id,operation_id,run_id,action,outcome,
      provider_id,provider_version,provider_model_sha256,worker_id_sha256,
      external_model_calls,rejection_code,rolling_window_seconds,
      max_reserved_calls,failure_threshold,reserved_calls_in_window,
      consecutive_rejections
    ) VALUES (
      new_reservation_id,actor,p_operation_id,p_run_id,'blocked',block_reason,
      p_provider_id,p_provider_version,p_provider_model_sha256,worker_sha,
      0,block_reason,p_rolling_window_seconds,p_max_reserved_calls,
      p_failure_threshold,reserved_count,rejection_count
    );
    RETURN QUERY SELECT
      NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,p_route,NULL::integer,
      NULL::text,NULL::text,NULL::text,NULL::text,NULL::text,
      NULL::timestamptz,NULL::timestamptz,NULL::text,NULL::integer,
      NULL::text,'{}'::jsonb,NULL::uuid,block_reason,reserved_count,
      rejection_count,'applied'::text;
    RETURN;
  END IF;

  WITH exhausted AS (
    SELECT extraction_job.job_id,extraction_job.status AS prior_status
    FROM memory.evidence_extraction_job AS extraction_job
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.route=p_route
      AND extraction_job.attempts>=p_max_attempts
      AND (
        extraction_job.status IN ('pending','error')
        OR (extraction_job.status='processing'
          AND extraction_job.lease_expires_at<=clock_timestamp())
      )
    ORDER BY extraction_job.priority,extraction_job.available_at,
      extraction_job.created_at,extraction_job.job_id
    FOR UPDATE SKIP LOCKED
    LIMIT 100
  ), terminalized AS (
    UPDATE memory.evidence_extraction_job AS extraction_job
    SET status='skipped',lease_token=NULL,lease_expires_at=NULL,
      worker_id=p_worker_id,last_error='maximum extraction attempts exhausted'
    FROM exhausted
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.job_id=exhausted.job_id
    RETURNING extraction_job.job_id,exhausted.prior_status,
      extraction_job.attempts
  )
  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,from_status,to_status,
    actor_type,actor_ref,details
  )
  SELECT actor,terminalized.job_id,gen_random_uuid(),'skipped',
    terminalized.prior_status,'skipped','worker',p_worker_id,
    jsonb_build_object('reason','max_attempts_exhausted',
      'attempt',terminalized.attempts,'max_attempts',p_max_attempts)
  FROM terminalized;

  WITH selected AS (
    SELECT extraction_job.job_id,extraction_job.status AS prior_status
    FROM memory.evidence_extraction_job AS extraction_job
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.route=p_route
      AND extraction_job.available_at<=clock_timestamp()
      AND extraction_job.attempts<p_max_attempts
      AND (
        extraction_job.status IN ('pending','error')
        OR (extraction_job.status='processing'
          AND extraction_job.lease_expires_at<=clock_timestamp())
      )
    ORDER BY extraction_job.priority,extraction_job.available_at,
      extraction_job.created_at,extraction_job.job_id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
  )
  UPDATE memory.evidence_extraction_job AS extraction_job
  SET status='processing',attempts=extraction_job.attempts+1,
    lease_token=new_lease_token,
    lease_expires_at=clock_timestamp()+make_interval(secs=>p_lease_seconds),
    worker_id=p_worker_id,last_error=NULL
  FROM selected
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=selected.job_id
  RETURNING extraction_job.*,selected.prior_status INTO claimed;

  IF NOT FOUND THEN
    RETURN;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id=actor
      AND evidence.evidence_id=claimed.evidence_id
      AND evidence.content IS NOT NULL
      AND evidence.content_sha256=claimed.evidence_content_sha256
  ) THEN
    RAISE EXCEPTION 'claimed evidence content binding is invalid'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_extraction_call_event(
    event_id,owner_user_id,operation_id,run_id,job_id,action,outcome,
    provider_id,provider_version,provider_model_sha256,worker_id_sha256,
    external_model_calls,rolling_window_seconds,max_reserved_calls,
    failure_threshold,reserved_calls_in_window,consecutive_rejections
  ) VALUES (
    new_reservation_id,actor,p_operation_id,p_run_id,claimed.job_id,
    'reserved','reserved',p_provider_id,p_provider_version,
    p_provider_model_sha256,worker_sha,1,p_rolling_window_seconds,
    p_max_reserved_calls,p_failure_threshold,reserved_count+1,rejection_count
  );

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,from_status,to_status,
    actor_type,actor_ref,details
  ) VALUES (
    actor,claimed.job_id,p_operation_id,'claimed',claimed.prior_status,
    'processing','worker',p_worker_id,
    jsonb_build_object('route',claimed.route,'lease_token',new_lease_token,
      'attempt',claimed.attempts,'lease_seconds',p_lease_seconds,
      'reclaimed',claimed.prior_status='processing',
      'call_reservation_event_id',new_reservation_id)
  );

  RETURN QUERY
  SELECT claimed.job_id,claimed.evidence_id,new_lease_token,
    claimed.status::text,claimed.route,claimed.attempts,
    claimed.evidence_content_sha256,evidence.kind::text,
    evidence.source_system,evidence.external_id,evidence.content,
    evidence.observed_at,evidence.recorded_at,evidence.sensitivity::text,
    claimed.checkpoint_sequence,claimed.checkpoint_sha256,claimed.result,
    new_reservation_id,'reserved'::text,reserved_count+1,rejection_count,
    'applied'::text
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=claimed.evidence_id;
END
$function$;
ALTER FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer) OWNER TO memory_v5_extraction_scheduler_maintainer;
REVOKE ALL ON FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer) FROM brains_app;
GRANT EXECUTE ON FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer) TO brains_app;
