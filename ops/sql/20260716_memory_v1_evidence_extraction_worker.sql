BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'evidence extraction worker migration requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regrole('memory_extraction_queue_maintainer') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL THEN
    RAISE EXCEPTION 'evidence extraction worker prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_extraction_worker_maintainer') IS NULL THEN
    CREATE ROLE memory_extraction_worker_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$role$;

ALTER ROLE memory_extraction_worker_maintainer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

ALTER TABLE memory.evidence_extraction_job
  ADD COLUMN IF NOT EXISTS checkpoint_sequence integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS checkpoint_sha256 text;

DO $job_constraints$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid='memory.evidence_extraction_job'::regclass
      AND conname='evidence_extraction_job_checkpoint_sequence_check'
  ) THEN
    ALTER TABLE memory.evidence_extraction_job
      ADD CONSTRAINT evidence_extraction_job_checkpoint_sequence_check
      CHECK (checkpoint_sequence>=0);
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid='memory.evidence_extraction_job'::regclass
      AND conname='evidence_extraction_job_checkpoint_binding_check'
  ) THEN
    ALTER TABLE memory.evidence_extraction_job
      ADD CONSTRAINT evidence_extraction_job_checkpoint_binding_check
      CHECK (
        (
          checkpoint_sequence=0
          AND checkpoint_sha256 IS NULL
        )
        OR
        (
          checkpoint_sequence>0
          AND checkpoint_sha256 ~ '^[0-9a-f]{64}$'
        )
      );
  END IF;
END
$job_constraints$;

ALTER TABLE memory.evidence_extraction_event
  ADD COLUMN IF NOT EXISTS operation_id uuid;

CREATE UNIQUE INDEX IF NOT EXISTS
  evidence_extraction_event_owner_operation_uidx
  ON memory.evidence_extraction_event(owner_user_id,operation_id)
  WHERE operation_id IS NOT NULL;

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

DO $function_ownership$
BEGIN
  IF to_regprocedure(
    'memory.claim_owner_evidence_extraction_job_v1(uuid,text,text,integer,integer)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.claim_owner_evidence_extraction_job_v1(
      uuid,text,text,integer,integer
    ) OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.checkpoint_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,integer,text,jsonb,integer)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.checkpoint_owner_evidence_extraction_job_v1(
      uuid,uuid,uuid,text,text,integer,text,jsonb,integer
    ) OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.finish_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,jsonb)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.finish_owner_evidence_extraction_job_v1(
      uuid,uuid,uuid,text,text,text,text,jsonb
    ) OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.fail_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,integer)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.fail_owner_evidence_extraction_job_v1(
      uuid,uuid,uuid,text,text,text,text,integer
    ) OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.resolve_owner_evidence_extraction_review_v1(uuid,uuid,text,text,text,jsonb)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.resolve_owner_evidence_extraction_review_v1(
      uuid,uuid,text,text,text,jsonb
    ) OWNER TO sage;
  END IF;
END
$function_ownership$;

CREATE OR REPLACE FUNCTION memory.claim_owner_evidence_extraction_job_v1(
  p_operation_id uuid,
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
  claimed record;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'evidence extraction claim requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_route NOT IN (
       'relational_extraction',
       'artifact_assessment',
       'structured_projection'
     )
     OR p_worker_id IS NULL
     OR btrim(p_worker_id)=''
     OR length(p_worker_id)>500
     OR p_lease_seconds NOT BETWEEN 30 AND 3600
     OR p_max_attempts NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'evidence extraction claim inputs are invalid'
      USING ERRCODE='22023';
  END IF;

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
       OR replayed.actor_ref IS DISTINCT FROM p_worker_id
       OR replayed.details->>'route' IS DISTINCT FROM p_route
       OR replayed.details->>'lease_token' IS NULL THEN
      RAISE EXCEPTION 'evidence extraction operation replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      replayed.job_id,
      replayed.evidence_id,
      (replayed.details->>'lease_token')::uuid,
      replayed.status::text,
      replayed.route,
      replayed.attempts,
      replayed.evidence_content_sha256,
      replayed.evidence_kind_value,
      replayed.evidence_source_system_value,
      replayed.evidence_external_id_value,
      replayed.evidence_content_value,
      replayed.evidence_observed_at_value,
      replayed.evidence_recorded_at_value,
      replayed.evidence_sensitivity_value,
      replayed.checkpoint_sequence,
      replayed.checkpoint_sha256,
      replayed.result,
      'replayed'::text;
    RETURN;
  END IF;

  WITH exhausted AS (
    SELECT
      extraction_job.job_id,
      extraction_job.status AS prior_status
    FROM memory.evidence_extraction_job AS extraction_job
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.route=p_route
      AND extraction_job.attempts>=p_max_attempts
      AND (
        extraction_job.status IN ('pending','error')
        OR (
          extraction_job.status='processing'
          AND extraction_job.lease_expires_at<=clock_timestamp()
        )
      )
    ORDER BY
      extraction_job.priority,
      extraction_job.available_at,
      extraction_job.created_at,
      extraction_job.job_id
    FOR UPDATE SKIP LOCKED
    LIMIT 100
  ),
  terminalized AS (
    UPDATE memory.evidence_extraction_job AS extraction_job
    SET
      status='skipped',
      lease_token=NULL,
      lease_expires_at=NULL,
      worker_id=p_worker_id,
      last_error='maximum extraction attempts exhausted'
    FROM exhausted
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.job_id=exhausted.job_id
    RETURNING
      extraction_job.job_id,
      exhausted.prior_status,
      extraction_job.attempts
  )
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
  )
  SELECT
    actor,
    terminalized.job_id,
    gen_random_uuid(),
    'skipped',
    terminalized.prior_status,
    'skipped',
    'worker',
    p_worker_id,
    jsonb_build_object(
      'reason','max_attempts_exhausted',
      'attempt',terminalized.attempts,
      'max_attempts',p_max_attempts
    )
  FROM terminalized;

  WITH selected AS (
    SELECT
      extraction_job.job_id,
      extraction_job.status AS prior_status
    FROM memory.evidence_extraction_job AS extraction_job
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.route=p_route
      AND extraction_job.available_at<=clock_timestamp()
      AND extraction_job.attempts<p_max_attempts
      AND (
        extraction_job.status IN ('pending','error')
        OR (
          extraction_job.status='processing'
          AND extraction_job.lease_expires_at<=clock_timestamp()
        )
      )
    ORDER BY
      extraction_job.priority,
      extraction_job.available_at,
      extraction_job.created_at,
      extraction_job.job_id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
  )
  UPDATE memory.evidence_extraction_job AS extraction_job
  SET
    status='processing',
    attempts=extraction_job.attempts+1,
    lease_token=new_lease_token,
    lease_expires_at=
      clock_timestamp()+make_interval(secs=>p_lease_seconds),
    worker_id=p_worker_id,
    last_error=NULL
  FROM selected
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=selected.job_id
  RETURNING
    extraction_job.*,
    selected.prior_status
  INTO claimed;

  IF NOT FOUND THEN
    RETURN;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id=actor
      AND evidence.evidence_id=claimed.evidence_id
      AND evidence.content IS NOT NULL
      AND evidence.content_sha256=claimed.evidence_content_sha256
  ) THEN
    RAISE EXCEPTION 'claimed evidence content binding is invalid'
      USING ERRCODE='23514';
  END IF;

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
    claimed.job_id,
    p_operation_id,
    'claimed',
    claimed.prior_status,
    'processing',
    'worker',
    p_worker_id,
    jsonb_build_object(
      'route',claimed.route,
      'lease_token',new_lease_token,
      'attempt',claimed.attempts,
      'lease_seconds',p_lease_seconds,
      'reclaimed',claimed.prior_status='processing'
    )
  );

  RETURN QUERY
  SELECT
    claimed.job_id,
    claimed.evidence_id,
    new_lease_token,
    claimed.status::text,
    claimed.route,
    claimed.attempts,
    claimed.evidence_content_sha256,
    evidence.kind::text,
    evidence.source_system,
    evidence.external_id,
    evidence.content,
    evidence.observed_at,
    evidence.recorded_at,
    evidence.sensitivity::text,
    claimed.checkpoint_sequence,
    claimed.checkpoint_sha256,
    claimed.result,
    'applied'::text
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=claimed.evidence_id;
END
$function$;

CREATE OR REPLACE FUNCTION memory.checkpoint_owner_evidence_extraction_job_v1(
  p_operation_id uuid,
  p_job_id uuid,
  p_lease_token uuid,
  p_worker_id text,
  p_expected_content_sha256 text,
  p_checkpoint_sequence integer,
  p_checkpoint_sha256 text,
  p_checkpoint jsonb,
  p_lease_seconds integer
)
RETURNS TABLE(
  job_id uuid,
  status text,
  checkpoint_sequence integer,
  checkpoint_sha256 text,
  lease_expires_at timestamptz,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  calculated_sha256 text;
  replayed record;
  current_job memory.evidence_extraction_job%ROWTYPE;
  next_result jsonb;
  next_lease_expires_at timestamptz;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'evidence extraction checkpoint requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_job_id IS NULL
     OR p_lease_token IS NULL
     OR p_worker_id IS NULL
     OR btrim(p_worker_id)=''
     OR length(p_worker_id)>500
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_checkpoint_sequence<1
     OR p_checkpoint_sha256 !~ '^[0-9a-f]{64}$'
     OR p_checkpoint IS NULL
     OR jsonb_typeof(p_checkpoint)<>'object'
     OR pg_column_size(p_checkpoint)>24576
     OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
    RAISE EXCEPTION 'evidence extraction checkpoint inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  calculated_sha256 := encode(
    public.digest(
      convert_to(p_checkpoint::text,'UTF8'),
      'sha256'
    ),
    'hex'
  );
  IF calculated_sha256<>p_checkpoint_sha256 THEN
    RAISE EXCEPTION 'evidence extraction checkpoint hash mismatch'
      USING ERRCODE='23514';
  END IF;

  SELECT
    event.job_id,
    event.event_type,
    event.to_status::text AS to_status,
    event.details
  INTO replayed
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.job_id<>p_job_id
       OR replayed.event_type<>'checkpointed'
       OR (replayed.details->>'checkpoint_sequence')::integer
          IS DISTINCT FROM p_checkpoint_sequence
       OR replayed.details->>'checkpoint_sha256'
          IS DISTINCT FROM p_checkpoint_sha256 THEN
      RAISE EXCEPTION 'evidence extraction operation replay conflicts'
        USING ERRCODE='23514';
    END IF;
    SELECT extraction_job.* INTO current_job
    FROM memory.evidence_extraction_job AS extraction_job
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.job_id=p_job_id;
    RETURN QUERY SELECT
      p_job_id,
      replayed.to_status,
      p_checkpoint_sequence,
      p_checkpoint_sha256,
      current_job.lease_expires_at,
      'replayed'::text;
    RETURN;
  END IF;

  SELECT extraction_job.* INTO current_job
  FROM memory.evidence_extraction_job AS extraction_job
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND
     OR current_job.status<>'processing'
     OR current_job.lease_token<>p_lease_token
     OR current_job.worker_id IS DISTINCT FROM p_worker_id
     OR current_job.lease_expires_at<=clock_timestamp()
     OR current_job.evidence_content_sha256
        IS DISTINCT FROM p_expected_content_sha256 THEN
    RAISE EXCEPTION 'evidence extraction lease or content binding is invalid'
      USING ERRCODE='23514';
  END IF;
  IF p_checkpoint_sequence<>current_job.checkpoint_sequence+1 THEN
    RAISE EXCEPTION 'evidence extraction checkpoint sequence is stale'
      USING ERRCODE='23514';
  END IF;

  next_result := jsonb_set(
    current_job.result,
    '{checkpoint}',
    jsonb_build_object(
      'sequence',p_checkpoint_sequence,
      'sha256',p_checkpoint_sha256,
      'payload',p_checkpoint
    ),
    true
  );
  IF pg_column_size(next_result)>32768 THEN
    RAISE EXCEPTION 'evidence extraction checkpoint exceeds result budget'
      USING ERRCODE='22023';
  END IF;
  next_lease_expires_at :=
    clock_timestamp()+make_interval(secs=>p_lease_seconds);

  UPDATE memory.evidence_extraction_job AS extraction_job
  SET
    checkpoint_sequence=p_checkpoint_sequence,
    checkpoint_sha256=p_checkpoint_sha256,
    result=next_result,
    lease_expires_at=next_lease_expires_at
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=p_job_id;

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
    'checkpointed',
    'processing',
    'processing',
    'worker',
    p_worker_id,
    jsonb_build_object(
      'checkpoint_sequence',p_checkpoint_sequence,
      'checkpoint_sha256',p_checkpoint_sha256,
      'lease_seconds',p_lease_seconds
    )
  );

  RETURN QUERY SELECT
    p_job_id,
    'processing'::text,
    p_checkpoint_sequence,
    p_checkpoint_sha256,
    next_lease_expires_at,
    'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION memory.finish_owner_evidence_extraction_job_v1(
  p_operation_id uuid,
  p_job_id uuid,
  p_lease_token uuid,
  p_worker_id text,
  p_expected_content_sha256 text,
  p_target_status text,
  p_result_sha256 text,
  p_result jsonb
)
RETURNS TABLE(
  job_id uuid,
  status text,
  result_sha256 text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  calculated_sha256 text;
  replayed record;
  current_job memory.evidence_extraction_job%ROWTYPE;
  next_result jsonb;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'evidence extraction finish requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_job_id IS NULL
     OR p_lease_token IS NULL
     OR p_worker_id IS NULL
     OR btrim(p_worker_id)=''
     OR length(p_worker_id)>500
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_target_status NOT IN ('review_required','completed','skipped')
     OR p_result_sha256 !~ '^[0-9a-f]{64}$'
     OR p_result IS NULL
     OR jsonb_typeof(p_result)<>'object'
     OR pg_column_size(p_result)>24576 THEN
    RAISE EXCEPTION 'evidence extraction finish inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  calculated_sha256 := encode(
    public.digest(convert_to(p_result::text,'UTF8'),'sha256'),
    'hex'
  );
  IF calculated_sha256<>p_result_sha256 THEN
    RAISE EXCEPTION 'evidence extraction result hash mismatch'
      USING ERRCODE='23514';
  END IF;

  SELECT
    event.job_id,
    event.event_type,
    event.to_status::text AS to_status,
    event.details
  INTO replayed
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.job_id<>p_job_id
       OR replayed.event_type<>p_target_status
       OR replayed.to_status<>p_target_status
       OR replayed.details->>'result_sha256'
          IS DISTINCT FROM p_result_sha256 THEN
      RAISE EXCEPTION 'evidence extraction operation replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      p_job_id,p_target_status,p_result_sha256,'replayed'::text;
    RETURN;
  END IF;

  SELECT extraction_job.* INTO current_job
  FROM memory.evidence_extraction_job AS extraction_job
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND
     OR current_job.status<>'processing'
     OR current_job.lease_token<>p_lease_token
     OR current_job.worker_id IS DISTINCT FROM p_worker_id
     OR current_job.lease_expires_at<=clock_timestamp()
     OR current_job.evidence_content_sha256
        IS DISTINCT FROM p_expected_content_sha256 THEN
    RAISE EXCEPTION 'evidence extraction lease or content binding is invalid'
      USING ERRCODE='23514';
  END IF;

  next_result := jsonb_set(
    current_job.result,
    '{final}',
    jsonb_build_object(
      'status',p_target_status,
      'sha256',p_result_sha256,
      'payload',p_result
    ),
    true
  );
  IF pg_column_size(next_result)>32768 THEN
    RAISE EXCEPTION 'evidence extraction result exceeds job budget'
      USING ERRCODE='22023';
  END IF;

  UPDATE memory.evidence_extraction_job AS extraction_job
  SET
    status=p_target_status::memory.evidence_extraction_job_status,
    lease_token=NULL,
    lease_expires_at=NULL,
    worker_id=p_worker_id,
    last_error=NULL,
    result=next_result
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=p_job_id;

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
    p_target_status,
    'processing',
    p_target_status::memory.evidence_extraction_job_status,
    'worker',
    p_worker_id,
    jsonb_build_object(
      'result_sha256',p_result_sha256,
      'checkpoint_sequence',current_job.checkpoint_sequence
    )
  );

  RETURN QUERY SELECT
    p_job_id,p_target_status,p_result_sha256,'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION memory.fail_owner_evidence_extraction_job_v1(
  p_operation_id uuid,
  p_job_id uuid,
  p_lease_token uuid,
  p_worker_id text,
  p_expected_content_sha256 text,
  p_error_class text,
  p_error_message text,
  p_max_attempts integer
)
RETURNS TABLE(
  job_id uuid,
  status text,
  attempts integer,
  available_at timestamptz,
  failure_sha256 text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  calculated_failure_sha256 text;
  failure_payload jsonb;
  replayed record;
  current_job memory.evidence_extraction_job%ROWTYPE;
  target_status text;
  retry_delay_seconds integer;
  next_available_at timestamptz;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'evidence extraction failure requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_job_id IS NULL
     OR p_lease_token IS NULL
     OR p_worker_id IS NULL
     OR btrim(p_worker_id)=''
     OR length(p_worker_id)>500
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_error_class IS NULL
     OR btrim(p_error_class)=''
     OR length(p_error_class)>200
     OR p_error_message IS NULL
     OR length(p_error_message)>1800
     OR p_max_attempts NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'evidence extraction failure inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  failure_payload := jsonb_build_object(
    'error_class',p_error_class,
    'error_message',p_error_message,
    'max_attempts',p_max_attempts
  );
  calculated_failure_sha256 := encode(
    public.digest(convert_to(failure_payload::text,'UTF8'),'sha256'),
    'hex'
  );

  SELECT
    event.job_id,
    event.event_type,
    event.to_status::text AS to_status,
    event.details
  INTO replayed
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.job_id<>p_job_id
       OR replayed.event_type NOT IN ('error','skipped')
       OR replayed.details->>'failure_sha256'
          IS DISTINCT FROM calculated_failure_sha256 THEN
      RAISE EXCEPTION 'evidence extraction operation replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      p_job_id,
      replayed.to_status,
      (replayed.details->>'attempt')::integer,
      NULLIF(replayed.details->>'available_at','')::timestamptz,
      calculated_failure_sha256,
      'replayed'::text;
    RETURN;
  END IF;

  SELECT extraction_job.* INTO current_job
  FROM memory.evidence_extraction_job AS extraction_job
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND
     OR current_job.status<>'processing'
     OR current_job.lease_token<>p_lease_token
     OR current_job.worker_id IS DISTINCT FROM p_worker_id
     OR current_job.lease_expires_at<=clock_timestamp()
     OR current_job.evidence_content_sha256
        IS DISTINCT FROM p_expected_content_sha256 THEN
    RAISE EXCEPTION 'evidence extraction lease or content binding is invalid'
      USING ERRCODE='23514';
  END IF;

  IF current_job.attempts>=p_max_attempts THEN
    target_status := 'skipped';
    retry_delay_seconds := 0;
    next_available_at := current_job.available_at;
  ELSE
    target_status := 'error';
    retry_delay_seconds := least(
      3600,
      (30*power(2::numeric,greatest(0,current_job.attempts-1)))::integer
    );
    next_available_at :=
      clock_timestamp()+make_interval(secs=>retry_delay_seconds);
  END IF;

  UPDATE memory.evidence_extraction_job AS extraction_job
  SET
    status=target_status::memory.evidence_extraction_job_status,
    lease_token=NULL,
    lease_expires_at=NULL,
    worker_id=p_worker_id,
    last_error=left(concat(p_error_class,': ',p_error_message),2000),
    available_at=next_available_at
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=p_job_id;

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
    target_status,
    'processing',
    target_status::memory.evidence_extraction_job_status,
    'worker',
    p_worker_id,
    jsonb_build_object(
      'failure_sha256',calculated_failure_sha256,
      'error_class',p_error_class,
      'attempt',current_job.attempts,
      'max_attempts',p_max_attempts,
      'retry_delay_seconds',retry_delay_seconds,
      'available_at',
        CASE
          WHEN target_status='error' THEN next_available_at::text
          ELSE ''
        END
    )
  );

  RETURN QUERY SELECT
    p_job_id,
    target_status,
    current_job.attempts,
    CASE WHEN target_status='error' THEN next_available_at ELSE NULL END,
    calculated_failure_sha256,
    'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION memory.resolve_owner_evidence_extraction_review_v1(
  p_operation_id uuid,
  p_job_id uuid,
  p_expected_result_sha256 text,
  p_target_status text,
  p_review_sha256 text,
  p_review jsonb
)
RETURNS TABLE(
  job_id uuid,
  status text,
  review_sha256 text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  calculated_sha256 text;
  replayed record;
  current_job memory.evidence_extraction_job%ROWTYPE;
  next_result jsonb;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'evidence extraction review requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_job_id IS NULL
     OR p_expected_result_sha256 !~ '^[0-9a-f]{64}$'
     OR p_target_status NOT IN ('completed','skipped')
     OR p_review_sha256 !~ '^[0-9a-f]{64}$'
     OR p_review IS NULL
     OR jsonb_typeof(p_review)<>'object'
     OR pg_column_size(p_review)>16384 THEN
    RAISE EXCEPTION 'evidence extraction review inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  calculated_sha256 := encode(
    public.digest(convert_to(p_review::text,'UTF8'),'sha256'),
    'hex'
  );
  IF calculated_sha256<>p_review_sha256 THEN
    RAISE EXCEPTION 'evidence extraction review hash mismatch'
      USING ERRCODE='23514';
  END IF;

  SELECT
    event.job_id,
    event.event_type,
    event.to_status::text AS to_status,
    event.details
  INTO replayed
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.job_id<>p_job_id
       OR replayed.event_type<>p_target_status
       OR replayed.to_status<>p_target_status
       OR replayed.details->>'review_sha256'
          IS DISTINCT FROM p_review_sha256 THEN
      RAISE EXCEPTION 'evidence extraction operation replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      p_job_id,p_target_status,p_review_sha256,'replayed'::text;
    RETURN;
  END IF;

  SELECT extraction_job.* INTO current_job
  FROM memory.evidence_extraction_job AS extraction_job
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND
     OR current_job.status<>'review_required'
     OR current_job.result#>>'{final,sha256}'
        IS DISTINCT FROM p_expected_result_sha256 THEN
    RAISE EXCEPTION 'evidence extraction review binding is invalid'
      USING ERRCODE='23514';
  END IF;

  next_result := jsonb_set(
    current_job.result,
    '{review}',
    jsonb_build_object(
      'status',p_target_status,
      'sha256',p_review_sha256,
      'payload',p_review
    ),
    true
  );
  IF pg_column_size(next_result)>32768 THEN
    RAISE EXCEPTION 'evidence extraction review exceeds job budget'
      USING ERRCODE='22023';
  END IF;

  UPDATE memory.evidence_extraction_job AS extraction_job
  SET
    status=p_target_status::memory.evidence_extraction_job_status,
    last_error=NULL,
    result=next_result
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=p_job_id;

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
    p_target_status,
    'review_required',
    p_target_status::memory.evidence_extraction_job_status,
    'admin',
    'owner_review',
    jsonb_build_object(
      'expected_result_sha256',p_expected_result_sha256,
      'review_sha256',p_review_sha256
    )
  );

  RETURN QUERY SELECT
    p_job_id,p_target_status,p_review_sha256,'applied'::text;
END
$function$;

GRANT USAGE ON SCHEMA memory
  TO memory_extraction_worker_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_extraction_worker_maintainer;
GRANT EXECUTE ON FUNCTION public.digest(bytea,text)
  TO memory_extraction_worker_maintainer;
GRANT SELECT,UPDATE ON memory.evidence_extraction_job
  TO memory_extraction_worker_maintainer;
GRANT SELECT,INSERT ON memory.evidence_extraction_event
  TO memory_extraction_worker_maintainer;
GRANT SELECT ON memory.evidence
  TO memory_extraction_worker_maintainer;

ALTER FUNCTION memory.claim_owner_evidence_extraction_job_v1(
  uuid,text,text,integer,integer
) OWNER TO memory_extraction_worker_maintainer;
ALTER FUNCTION memory.checkpoint_owner_evidence_extraction_job_v1(
  uuid,uuid,uuid,text,text,integer,text,jsonb,integer
) OWNER TO memory_extraction_worker_maintainer;
ALTER FUNCTION memory.finish_owner_evidence_extraction_job_v1(
  uuid,uuid,uuid,text,text,text,text,jsonb
) OWNER TO memory_extraction_worker_maintainer;
ALTER FUNCTION memory.fail_owner_evidence_extraction_job_v1(
  uuid,uuid,uuid,text,text,text,text,integer
) OWNER TO memory_extraction_worker_maintainer;
ALTER FUNCTION memory.resolve_owner_evidence_extraction_review_v1(
  uuid,uuid,text,text,text,jsonb
) OWNER TO memory_extraction_worker_maintainer;

REVOKE ALL ON FUNCTION memory.claim_owner_evidence_extraction_job_v1(
  uuid,text,text,integer,integer
) FROM PUBLIC,brains_app,memory_extraction_worker_maintainer;
REVOKE ALL ON FUNCTION memory.checkpoint_owner_evidence_extraction_job_v1(
  uuid,uuid,uuid,text,text,integer,text,jsonb,integer
) FROM PUBLIC,brains_app,memory_extraction_worker_maintainer;
REVOKE ALL ON FUNCTION memory.finish_owner_evidence_extraction_job_v1(
  uuid,uuid,uuid,text,text,text,text,jsonb
) FROM PUBLIC,brains_app,memory_extraction_worker_maintainer;
REVOKE ALL ON FUNCTION memory.fail_owner_evidence_extraction_job_v1(
  uuid,uuid,uuid,text,text,text,text,integer
) FROM PUBLIC,brains_app,memory_extraction_worker_maintainer;
REVOKE ALL ON FUNCTION memory.resolve_owner_evidence_extraction_review_v1(
  uuid,uuid,text,text,text,jsonb
) FROM PUBLIC,brains_app,memory_extraction_worker_maintainer;

GRANT EXECUTE ON FUNCTION memory.claim_owner_evidence_extraction_job_v1(
  uuid,text,text,integer,integer
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.checkpoint_owner_evidence_extraction_job_v1(
  uuid,uuid,uuid,text,text,integer,text,jsonb,integer
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.finish_owner_evidence_extraction_job_v1(
  uuid,uuid,uuid,text,text,text,text,jsonb
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.fail_owner_evidence_extraction_job_v1(
  uuid,uuid,uuid,text,text,text,text,integer
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.resolve_owner_evidence_extraction_review_v1(
  uuid,uuid,text,text,text,jsonb
) TO brains_app;

COMMIT;
