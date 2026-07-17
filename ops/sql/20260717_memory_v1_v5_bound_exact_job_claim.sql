BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage'
     OR to_regrole('brains_app') IS NULL
     OR to_regrole('memory_extraction_worker_maintainer') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.project_thread_binding_event') IS NULL
     OR to_regclass('memory.current_project_thread_binding_v5') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL THEN
    RAISE EXCEPTION 'bound exact-job claim prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.claim_owner_bound_evidence_job_v5(
  p_operation_id uuid,
  p_job_id uuid,
  p_expected_content_sha256 text,
  p_project_binding_event_id uuid,
  p_route text,
  p_worker_id text,
  p_lease_seconds integer,
  p_max_attempts integer
)
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
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  new_lease_token uuid := gen_random_uuid();
  replayed record;
  current_job memory.evidence_extraction_job%ROWTYPE;
  prior_status memory.evidence_extraction_job_status;
  evidence_record memory.evidence%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'bound exact-job claim requires brains_app session'
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
     OR p_route<>'relational_extraction'
     OR p_worker_id IS NULL
     OR btrim(p_worker_id)=''
     OR length(p_worker_id)>500
     OR p_lease_seconds NOT BETWEEN 30 AND 3600
     OR p_max_attempts NOT BETWEEN 1 AND 3 THEN
    RAISE EXCEPTION 'bound exact-job claim inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||':bound_exact_job:'||p_job_id::text,0
  ));

  SELECT
    event.event_type,
    event.actor_ref,
    event.details,
    extraction_job.*,
    evidence.kind::text AS evidence_kind_value,
    evidence.source_system AS evidence_source_system_value,
    evidence.external_id AS evidence_external_id_value,
    evidence.content AS evidence_content_value,
    evidence.observed_at AS evidence_observed_at_value,
    evidence.recorded_at AS evidence_recorded_at_value,
    evidence.sensitivity::text AS evidence_sensitivity_value
  INTO replayed
  FROM memory.evidence_extraction_event AS event
  JOIN memory.evidence_extraction_job AS extraction_job
    ON extraction_job.owner_user_id=event.owner_user_id
   AND extraction_job.job_id=event.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=extraction_job.owner_user_id
   AND evidence.evidence_id=extraction_job.evidence_id
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.event_type<>'claimed'
       OR replayed.job_id<>p_job_id
       OR replayed.actor_ref IS DISTINCT FROM p_worker_id
       OR replayed.details->>'route' IS DISTINCT FROM p_route
       OR replayed.details->>'expected_content_sha256'
          IS DISTINCT FROM p_expected_content_sha256
       OR (replayed.details->>'project_binding_event_id')::uuid
          IS DISTINCT FROM p_project_binding_event_id
       OR replayed.details->>'lease_token' IS NULL THEN
      RAISE EXCEPTION 'bound exact-job operation replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      replayed.job_id,replayed.evidence_id,
      (replayed.details->>'lease_token')::uuid,
      replayed.status::text,replayed.route,replayed.attempts,
      replayed.evidence_content_sha256,
      replayed.evidence_kind_value,replayed.evidence_source_system_value,
      replayed.evidence_external_id_value,replayed.evidence_content_value,
      replayed.evidence_observed_at_value,replayed.evidence_recorded_at_value,
      replayed.evidence_sensitivity_value,replayed.checkpoint_sequence,
      replayed.checkpoint_sha256,replayed.result,'replayed'::text;
    RETURN;
  END IF;

  SELECT extraction_job.* INTO current_job
  FROM memory.evidence_extraction_job AS extraction_job
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND
     OR current_job.route<>p_route
     OR current_job.evidence_content_sha256<>p_expected_content_sha256
     OR current_job.available_at>clock_timestamp()
     OR current_job.attempts>=p_max_attempts
     OR NOT (
       current_job.status IN ('pending','error')
       OR (
         current_job.status='processing'
         AND current_job.lease_expires_at<=clock_timestamp()
       )
     ) THEN
    RAISE EXCEPTION 'bound exact-job is absent or ineligible'
      USING ERRCODE='23514';
  END IF;
  prior_status := current_job.status;

  SELECT evidence.* INTO evidence_record
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=current_job.evidence_id
    AND evidence.content IS NOT NULL
    AND evidence.content_sha256=p_expected_content_sha256;
  IF NOT FOUND
     OR evidence_record.metadata->>'thread_id' IS NULL
     OR NOT EXISTS (
       SELECT 1
       FROM memory.current_project_thread_binding_v5 AS binding
       WHERE binding.owner_user_id=actor
         AND binding.binding_event_id=p_project_binding_event_id
         AND binding.thread_id::text=evidence_record.metadata->>'thread_id'
     ) THEN
    RAISE EXCEPTION 'exact-job evidence is not covered by the reviewed binding'
      USING ERRCODE='23514';
  END IF;

  UPDATE memory.evidence_extraction_job AS extraction_job
  SET
    status='processing',
    attempts=extraction_job.attempts+1,
    lease_token=new_lease_token,
    lease_expires_at=clock_timestamp()+make_interval(secs=>p_lease_seconds),
    worker_id=p_worker_id,
    last_error=NULL
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=p_job_id
  RETURNING extraction_job.* INTO current_job;

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,from_status,to_status,
    actor_type,actor_ref,details
  ) VALUES (
    actor,p_job_id,p_operation_id,'claimed',prior_status,'processing',
    'worker',p_worker_id,jsonb_build_object(
      'route',p_route,
      'lease_token',new_lease_token,
      'attempt',current_job.attempts,
      'lease_seconds',p_lease_seconds,
      'exact_job_canary',true,
      'expected_content_sha256',p_expected_content_sha256,
      'project_binding_event_id',p_project_binding_event_id,
      'reclaimed',prior_status='processing'
    )
  );

  RETURN QUERY SELECT
    current_job.job_id,current_job.evidence_id,new_lease_token,
    current_job.status::text,current_job.route,current_job.attempts,
    current_job.evidence_content_sha256,evidence_record.kind::text,
    evidence_record.source_system,evidence_record.external_id,
    evidence_record.content,evidence_record.observed_at,
    evidence_record.recorded_at,evidence_record.sensitivity::text,
    current_job.checkpoint_sequence,current_job.checkpoint_sha256,
    current_job.result,'applied'::text;
END
$function$;

GRANT SELECT ON
  memory.project_thread_binding_event,
  memory.current_project_thread_binding_v5
TO memory_extraction_worker_maintainer;

ALTER FUNCTION memory.claim_owner_bound_evidence_job_v5(
  uuid,uuid,text,uuid,text,text,integer,integer
) OWNER TO memory_extraction_worker_maintainer;

REVOKE ALL ON FUNCTION memory.claim_owner_bound_evidence_job_v5(
  uuid,uuid,text,uuid,text,text,integer,integer
) FROM PUBLIC,brains_app,memory_extraction_worker_maintainer;
GRANT EXECUTE ON FUNCTION memory.claim_owner_bound_evidence_job_v5(
  uuid,uuid,text,uuid,text,text,integer,integer
) TO brains_app;

COMMIT;
