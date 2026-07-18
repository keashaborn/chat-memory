BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 extraction call ledger migration requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure(
       'memory.fail_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5 extraction call ledger prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_v5_extraction_scheduler_maintainer') IS NULL THEN
    CREATE ROLE memory_v5_extraction_scheduler_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$role$;

ALTER ROLE memory_v5_extraction_scheduler_maintainer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

CREATE TABLE IF NOT EXISTS memory.v5_extraction_call_event (
  event_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  run_id uuid NOT NULL,
  job_id uuid,
  reservation_event_id uuid,
  action text NOT NULL,
  outcome text NOT NULL,
  provider_id text NOT NULL,
  provider_version text NOT NULL,
  provider_model_sha256 text NOT NULL,
  worker_id_sha256 text NOT NULL,
  external_model_calls smallint NOT NULL,
  rejection_code text,
  provider_output_sha256 text,
  validator_packet_sha256 text,
  packet_storage_sha256 text,
  rolling_window_seconds integer NOT NULL,
  max_reserved_calls integer NOT NULL,
  failure_threshold integer NOT NULL,
  reserved_calls_in_window integer NOT NULL,
  consecutive_rejections integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,event_id),
  UNIQUE (owner_user_id,operation_id),
  FOREIGN KEY (owner_user_id,job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id,job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,reservation_event_id)
    REFERENCES memory.v5_extraction_call_event(owner_user_id,event_id)
    ON DELETE RESTRICT,
  CHECK (action IN ('blocked','reserved','completed')),
  CHECK (outcome IN (
    'quota_exhausted','circuit_open','reserved','accepted','rejected'
  )),
  CHECK (provider_id='openai_responses'),
  CHECK (provider_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'),
  CHECK (provider_model_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (worker_id_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (external_model_calls BETWEEN 0 AND 1),
  CHECK (
    rejection_code IS NULL
    OR rejection_code ~ '^[a-z][a-z0-9_]{1,99}$'
  ),
  CHECK (
    provider_output_sha256 IS NULL
    OR provider_output_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CHECK (
    validator_packet_sha256 IS NULL
    OR validator_packet_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CHECK (
    packet_storage_sha256 IS NULL
    OR packet_storage_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CHECK (rolling_window_seconds BETWEEN 3600 AND 604800),
  CHECK (max_reserved_calls BETWEEN 1 AND 100),
  CHECK (failure_threshold BETWEEN 1 AND 10),
  CHECK (reserved_calls_in_window BETWEEN 0 AND 100),
  CHECK (consecutive_rejections BETWEEN 0 AND 10),
  CHECK (
    (action='blocked'
      AND job_id IS NULL
      AND reservation_event_id IS NULL
      AND outcome IN ('quota_exhausted','circuit_open')
      AND external_model_calls=0
      AND rejection_code=outcome
      AND provider_output_sha256 IS NULL
      AND validator_packet_sha256 IS NULL
      AND packet_storage_sha256 IS NULL)
    OR
    (action='reserved'
      AND job_id IS NOT NULL
      AND reservation_event_id IS NULL
      AND outcome='reserved'
      AND external_model_calls=1
      AND rejection_code IS NULL
      AND provider_output_sha256 IS NULL
      AND validator_packet_sha256 IS NULL
      AND packet_storage_sha256 IS NULL)
    OR
    (action='completed'
      AND job_id IS NOT NULL
      AND reservation_event_id IS NOT NULL
      AND outcome IN ('accepted','rejected')
      AND (
        (outcome='accepted'
          AND external_model_calls=1
          AND rejection_code IS NULL
          AND provider_output_sha256 IS NOT NULL
          AND validator_packet_sha256 IS NOT NULL
          AND packet_storage_sha256 IS NOT NULL)
        OR
        (outcome='rejected'
          AND rejection_code IS NOT NULL
          AND provider_output_sha256 IS NULL
          AND validator_packet_sha256 IS NULL
          AND packet_storage_sha256 IS NULL)
      ))
  )
);

CREATE INDEX IF NOT EXISTS v5_extraction_call_owner_time_idx
  ON memory.v5_extraction_call_event(
    owner_user_id,created_at DESC,event_id DESC
  );
CREATE INDEX IF NOT EXISTS v5_extraction_call_reservation_idx
  ON memory.v5_extraction_call_event(
    owner_user_id,reservation_event_id
  ) WHERE reservation_event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS v5_extraction_call_one_completion_uq
  ON memory.v5_extraction_call_event(
    owner_user_id,reservation_event_id
  ) WHERE action='completed';

ALTER TABLE memory.v5_extraction_call_event OWNER TO sage;

CREATE OR REPLACE FUNCTION memory.guard_v5_extraction_call_append_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'memory.v5_extraction_call_event is append-only'
    USING ERRCODE='42501';
END
$function$;

DROP TRIGGER IF EXISTS v5_extraction_call_append_only_guard
  ON memory.v5_extraction_call_event;
CREATE TRIGGER v5_extraction_call_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_extraction_call_event
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_extraction_call_append_only();

ALTER TABLE memory.v5_extraction_call_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_extraction_call_event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.v5_extraction_call_event;
CREATE POLICY owner_isolation ON memory.v5_extraction_call_event
  USING (owner_user_id=(SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()));

CREATE OR REPLACE FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(
  p_operation_id uuid,
  p_run_id uuid,
  p_route text,
  p_worker_id text,
  p_lease_seconds integer,
  p_max_attempts integer,
  p_provider_id text,
  p_provider_version text,
  p_provider_model_sha256 text,
  p_rolling_window_seconds integer,
  p_max_reserved_calls integer,
  p_failure_threshold integer
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
  reservation_event_id uuid,
  control_outcome text,
  reserved_calls_in_window integer,
  consecutive_rejections integer,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
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

CREATE OR REPLACE FUNCTION memory.complete_owner_v5_extraction_call_v1(
  p_operation_id uuid,
  p_reservation_event_id uuid,
  p_run_id uuid,
  p_job_id uuid,
  p_outcome text,
  p_external_model_calls integer,
  p_rejection_code text,
  p_provider_output_sha256 text,
  p_validator_packet_sha256 text,
  p_packet_storage_sha256 text
)
RETURNS TABLE(
  event_id uuid,
  outcome text,
  external_model_calls integer,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  reservation memory.v5_extraction_call_event%ROWTYPE;
  replayed memory.v5_extraction_call_event%ROWTYPE;
  new_event_id uuid := gen_random_uuid();
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5 extraction call completion requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_reservation_event_id IS NULL
     OR p_run_id IS NULL
     OR p_job_id IS NULL
     OR p_outcome NOT IN ('accepted','rejected')
     OR p_external_model_calls NOT BETWEEN 0 AND 1
     OR (p_outcome='accepted' AND (
       p_external_model_calls<>1 OR p_rejection_code IS NOT NULL
       OR p_provider_output_sha256 !~ '^[0-9a-f]{64}$'
       OR p_validator_packet_sha256 !~ '^[0-9a-f]{64}$'
       OR p_packet_storage_sha256 !~ '^[0-9a-f]{64}$'))
     OR (p_outcome='rejected' AND (
       p_rejection_code IS NULL
       OR p_rejection_code !~ '^[a-z][a-z0-9_]{1,99}$'
       OR p_provider_output_sha256 IS NOT NULL
       OR p_validator_packet_sha256 IS NOT NULL
       OR p_packet_storage_sha256 IS NOT NULL)) THEN
    RAISE EXCEPTION 'V5 extraction call completion inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT event.* INTO replayed
  FROM memory.v5_extraction_call_event AS event
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.action<>'completed'
       OR replayed.reservation_event_id<>p_reservation_event_id
       OR replayed.run_id<>p_run_id
       OR replayed.job_id<>p_job_id
       OR replayed.outcome<>p_outcome
       OR replayed.external_model_calls<>p_external_model_calls
       OR replayed.rejection_code IS DISTINCT FROM p_rejection_code
       OR replayed.provider_output_sha256
          IS DISTINCT FROM p_provider_output_sha256
       OR replayed.validator_packet_sha256
          IS DISTINCT FROM p_validator_packet_sha256
       OR replayed.packet_storage_sha256
          IS DISTINCT FROM p_packet_storage_sha256 THEN
      RAISE EXCEPTION 'V5 extraction completion replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.event_id,replayed.outcome,
      replayed.external_model_calls::integer,'replayed'::text;
    RETURN;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_v5_call_completion',actor::text,
      p_reservation_event_id::text),0
  ));
  SELECT event.* INTO reservation
  FROM memory.v5_extraction_call_event AS event
  WHERE event.owner_user_id=actor
    AND event.event_id=p_reservation_event_id
    AND event.action='reserved'
    AND event.run_id=p_run_id
    AND event.job_id=p_job_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'V5 extraction call reservation is absent or mismatched'
      USING ERRCODE='23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.v5_extraction_call_event AS event
    WHERE event.owner_user_id=actor
      AND event.reservation_event_id=p_reservation_event_id
      AND event.action='completed'
  ) THEN
    RAISE EXCEPTION 'V5 extraction call reservation is already completed'
      USING ERRCODE='23514';
  END IF;
  IF p_outcome='accepted' AND NOT EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_packet_v5 AS packet
    JOIN memory.evidence_extraction_job AS extraction_job
      ON extraction_job.owner_user_id=packet.owner_user_id
     AND extraction_job.job_id=packet.job_id
    WHERE packet.owner_user_id=actor
      AND packet.job_id=p_job_id
      AND packet.external_model_calls=1
      AND packet.provider_output_sha256=p_provider_output_sha256
      AND packet.validator_packet_sha256=p_validator_packet_sha256
      AND packet.packet_storage_sha256=p_packet_storage_sha256
      AND extraction_job.status='review_required'
  ) THEN
    RAISE EXCEPTION 'accepted V5 extraction call lacks its durable packet'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_extraction_call_event(
    event_id,owner_user_id,operation_id,run_id,job_id,
    reservation_event_id,action,outcome,provider_id,provider_version,
    provider_model_sha256,worker_id_sha256,external_model_calls,
    rejection_code,provider_output_sha256,validator_packet_sha256,
    packet_storage_sha256,rolling_window_seconds,max_reserved_calls,
    failure_threshold,reserved_calls_in_window,consecutive_rejections
  ) VALUES (
    new_event_id,actor,p_operation_id,p_run_id,p_job_id,
    p_reservation_event_id,'completed',p_outcome,reservation.provider_id,
    reservation.provider_version,reservation.provider_model_sha256,
    reservation.worker_id_sha256,p_external_model_calls,p_rejection_code,
    p_provider_output_sha256,p_validator_packet_sha256,
    p_packet_storage_sha256,reservation.rolling_window_seconds,
    reservation.max_reserved_calls,reservation.failure_threshold,
    reservation.reserved_calls_in_window,
    CASE WHEN p_outcome='rejected'
      THEN least(reservation.consecutive_rejections+1,10) ELSE 0 END
  );
  RETURN QUERY SELECT new_event_id,p_outcome,p_external_model_calls,
    'applied'::text;
END
$function$;

GRANT USAGE ON SCHEMA memory TO memory_v5_extraction_scheduler_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_extraction_scheduler_maintainer;
GRANT EXECUTE ON FUNCTION public.digest(bytea,text)
  TO memory_v5_extraction_scheduler_maintainer;
GRANT SELECT,UPDATE ON memory.evidence_extraction_job
  TO memory_v5_extraction_scheduler_maintainer;
GRANT SELECT,INSERT ON memory.evidence_extraction_event
  TO memory_v5_extraction_scheduler_maintainer;
GRANT SELECT ON memory.evidence TO memory_v5_extraction_scheduler_maintainer;
GRANT SELECT ON memory.evidence_extraction_packet_v5
  TO memory_v5_extraction_scheduler_maintainer;
GRANT SELECT,INSERT ON memory.v5_extraction_call_event
  TO memory_v5_extraction_scheduler_maintainer;

ALTER FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(
  uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer
) OWNER TO memory_v5_extraction_scheduler_maintainer;
ALTER FUNCTION memory.complete_owner_v5_extraction_call_v1(
  uuid,uuid,uuid,uuid,text,integer,text,text,text,text
) OWNER TO memory_v5_extraction_scheduler_maintainer;

REVOKE ALL ON TABLE memory.v5_extraction_call_event
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(
  uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer
) FROM PUBLIC,brains_app,memory_v5_extraction_scheduler_maintainer;
REVOKE ALL ON FUNCTION memory.complete_owner_v5_extraction_call_v1(
  uuid,uuid,uuid,uuid,text,integer,text,text,text,text
) FROM PUBLIC,brains_app,memory_v5_extraction_scheduler_maintainer;

GRANT EXECUTE ON FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(
  uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.complete_owner_v5_extraction_call_v1(
  uuid,uuid,uuid,uuid,text,integer,text,text,text,text
) TO brains_app;

COMMIT;
