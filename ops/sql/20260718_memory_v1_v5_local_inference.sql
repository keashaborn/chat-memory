BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 local inference migration requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regrole('memory_extraction_worker_maintainer') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure(
       'memory.claim_owner_evidence_extraction_job_v1(uuid,text,text,integer,integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5 local inference prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_v5_local_inference_maintainer') IS NULL THEN
    CREATE ROLE memory_v5_local_inference_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$role$;

ALTER ROLE memory_v5_local_inference_maintainer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

CREATE TABLE IF NOT EXISTS memory.v5_local_inference_event (
  event_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  run_id uuid NOT NULL,
  job_id uuid,
  target_job_sha256 text NOT NULL,
  target_content_sha256 text NOT NULL,
  reservation_event_id uuid,
  action text NOT NULL,
  outcome text NOT NULL,
  provider_id text NOT NULL,
  provider_version text NOT NULL,
  provider_model_sha256 text NOT NULL,
  model_file_sha256 text NOT NULL,
  runtime_revision_sha256 text NOT NULL,
  policy_compiler_sha256 text NOT NULL,
  worker_id_sha256 text NOT NULL,
  local_model_calls smallint NOT NULL,
  external_model_calls smallint NOT NULL,
  rejection_code text,
  provider_output_sha256 text,
  validator_packet_sha256 text,
  packet_storage_sha256 text,
  rolling_window_seconds integer NOT NULL,
  max_reserved_jobs integer NOT NULL,
  failure_threshold integer NOT NULL,
  reserved_jobs_in_window integer NOT NULL,
  consecutive_rejections integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,event_id),
  UNIQUE (owner_user_id,operation_id),
  FOREIGN KEY (owner_user_id,job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id,job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,reservation_event_id)
    REFERENCES memory.v5_local_inference_event(owner_user_id,event_id)
    ON DELETE RESTRICT,
  CHECK (action IN ('blocked','reserved','completed')),
  CHECK (outcome IN (
    'quota_exhausted','circuit_open','reserved','accepted','rejected'
  )),
  CHECK (provider_id='local_llama_cpp'),
  CHECK (target_job_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (target_content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (provider_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'),
  CHECK (provider_model_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (model_file_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (runtime_revision_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (policy_compiler_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (worker_id_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (local_model_calls BETWEEN 0 AND 1),
  CHECK (external_model_calls=0),
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
  CHECK (max_reserved_jobs BETWEEN 1 AND 100),
  CHECK (failure_threshold BETWEEN 1 AND 10),
  CHECK (reserved_jobs_in_window BETWEEN 0 AND 100),
  CHECK (consecutive_rejections BETWEEN 0 AND 10),
  CHECK (
    (action='blocked'
      AND job_id IS NULL
      AND reservation_event_id IS NULL
      AND outcome IN ('quota_exhausted','circuit_open')
      AND local_model_calls=0
      AND rejection_code=outcome
      AND provider_output_sha256 IS NULL
      AND validator_packet_sha256 IS NULL
      AND packet_storage_sha256 IS NULL)
    OR
    (action='reserved'
      AND job_id IS NOT NULL
      AND reservation_event_id IS NULL
      AND outcome='reserved'
      AND local_model_calls=0
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

CREATE INDEX IF NOT EXISTS v5_local_inference_owner_time_idx
  ON memory.v5_local_inference_event(
    owner_user_id,created_at DESC,event_id DESC
  );
CREATE UNIQUE INDEX IF NOT EXISTS v5_local_inference_one_completion_uq
  ON memory.v5_local_inference_event(
    owner_user_id,reservation_event_id
  ) WHERE action='completed';

CREATE TABLE IF NOT EXISTS memory.evidence_extraction_packet_v5_local (
  packet_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  job_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  evidence_content_sha256 text NOT NULL,
  provider_id text NOT NULL,
  provider_version text NOT NULL,
  provider_model_sha256 text NOT NULL,
  model_file_sha256 text NOT NULL,
  runtime_revision_sha256 text NOT NULL,
  policy_compiler_sha256 text NOT NULL,
  provider_output_sha256 text NOT NULL,
  validator_packet_sha256 text NOT NULL,
  packet_storage_sha256 text NOT NULL,
  normalized_packet jsonb NOT NULL,
  manual_review_required boolean NOT NULL,
  local_model_calls smallint NOT NULL,
  external_model_calls smallint NOT NULL,
  entity_mention_count integer NOT NULL,
  observation_count integer NOT NULL,
  comparison_hint_count integer NOT NULL,
  deferral_count integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,packet_id),
  UNIQUE (owner_user_id,operation_id),
  UNIQUE (owner_user_id,job_id),
  FOREIGN KEY (owner_user_id,job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id,job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  CHECK (evidence_content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (provider_id='local_llama_cpp'),
  CHECK (provider_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'),
  CHECK (provider_model_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (model_file_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (runtime_revision_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (policy_compiler_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (provider_output_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (validator_packet_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (packet_storage_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (jsonb_typeof(normalized_packet)='object'),
  CHECK (pg_column_size(normalized_packet)<=262144),
  CHECK (local_model_calls BETWEEN 0 AND 1),
  CHECK (external_model_calls=0),
  CHECK (entity_mention_count BETWEEN 0 AND 24),
  CHECK (observation_count BETWEEN 0 AND 32),
  CHECK (comparison_hint_count BETWEEN 0 AND 32),
  CHECK (deferral_count BETWEEN 0 AND 32)
);

CREATE INDEX IF NOT EXISTS evidence_extraction_packet_v5_local_owner_time_idx
  ON memory.evidence_extraction_packet_v5_local(
    owner_user_id,created_at DESC,packet_id
  );

ALTER TABLE memory.v5_local_inference_event OWNER TO sage;
ALTER TABLE memory.evidence_extraction_packet_v5_local OWNER TO sage;

CREATE OR REPLACE FUNCTION memory.guard_v5_local_inference_append_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION '% is append-only',TG_TABLE_SCHEMA||'.'||TG_TABLE_NAME
    USING ERRCODE='42501';
END
$function$;

DROP TRIGGER IF EXISTS v5_local_inference_event_append_only_guard
  ON memory.v5_local_inference_event;
CREATE TRIGGER v5_local_inference_event_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_inference_event
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

DROP TRIGGER IF EXISTS v5_local_packet_append_only_guard
  ON memory.evidence_extraction_packet_v5_local;
CREATE TRIGGER v5_local_packet_append_only_guard
BEFORE UPDATE OR DELETE ON memory.evidence_extraction_packet_v5_local
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

ALTER TABLE memory.v5_local_inference_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_inference_event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.v5_local_inference_event;
CREATE POLICY owner_isolation ON memory.v5_local_inference_event
  USING (owner_user_id=(SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()));

ALTER TABLE memory.evidence_extraction_packet_v5_local ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.evidence_extraction_packet_v5_local FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.evidence_extraction_packet_v5_local;
CREATE POLICY owner_isolation ON memory.evidence_extraction_packet_v5_local
  USING (owner_user_id=(SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()));

CREATE OR REPLACE FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  p_operation_id uuid,
  p_run_id uuid,
  p_expected_job_id uuid,
  p_expected_content_sha256 text,
  p_route text,
  p_worker_id text,
  p_lease_seconds integer,
  p_max_attempts integer,
  p_provider_id text,
  p_provider_version text,
  p_provider_model_sha256 text,
  p_model_file_sha256 text,
  p_runtime_revision_sha256 text,
  p_policy_compiler_sha256 text,
  p_rolling_window_seconds integer,
  p_max_reserved_jobs integer,
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
  reserved_jobs_in_window integer,
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
  target_job_sha text;
  reservation_id uuid := gen_random_uuid();
  replayed memory.v5_local_inference_event%ROWTYPE;
  claimed record;
  reserved_count integer;
  latest_count integer;
  latest_all_rejected boolean;
  rejection_count integer;
  block_reason text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5 local inference claim requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_run_id IS NULL
     OR p_expected_job_id IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_route<>'relational_extraction'
     OR p_worker_id IS NULL
     OR btrim(p_worker_id)=''
     OR length(p_worker_id)>500
     OR p_lease_seconds NOT BETWEEN 30 AND 3600
     OR p_max_attempts NOT BETWEEN 1 AND 3
     OR p_provider_id<>'local_llama_cpp'
     OR p_provider_version !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
     OR p_provider_model_sha256 !~ '^[0-9a-f]{64}$'
     OR p_model_file_sha256 !~ '^[0-9a-f]{64}$'
     OR p_runtime_revision_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_compiler_sha256 !~ '^[0-9a-f]{64}$'
     OR p_rolling_window_seconds NOT BETWEEN 3600 AND 604800
     OR p_max_reserved_jobs NOT BETWEEN 1 AND 100
     OR p_failure_threshold NOT BETWEEN 1 AND 10 THEN
    RAISE EXCEPTION 'V5 local inference claim inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  worker_sha := encode(
    public.digest(convert_to(p_worker_id,'UTF8'),'sha256'),'hex'
  );
  target_job_sha := encode(
    public.digest(convert_to(p_expected_job_id::text,'UTF8'),'sha256'),'hex'
  );
  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_v5_local_inference',actor::text,p_route),0
  ));

  SELECT event.* INTO replayed
  FROM memory.v5_local_inference_event AS event
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.run_id<>p_run_id
       OR replayed.action NOT IN ('blocked','reserved')
       OR replayed.target_job_sha256<>target_job_sha
       OR replayed.target_content_sha256<>p_expected_content_sha256
       OR replayed.provider_id<>p_provider_id
       OR replayed.provider_version<>p_provider_version
       OR replayed.provider_model_sha256<>p_provider_model_sha256
       OR replayed.model_file_sha256<>p_model_file_sha256
       OR replayed.runtime_revision_sha256<>p_runtime_revision_sha256
       OR replayed.policy_compiler_sha256<>p_policy_compiler_sha256
       OR replayed.worker_id_sha256<>worker_sha
       OR replayed.rolling_window_seconds<>p_rolling_window_seconds
       OR replayed.max_reserved_jobs<>p_max_reserved_jobs
       OR replayed.failure_threshold<>p_failure_threshold THEN
      RAISE EXCEPTION 'V5 local inference claim replay conflicts'
        USING ERRCODE='23514';
    END IF;
    IF replayed.action='blocked' THEN
      RETURN QUERY SELECT
        NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,p_route,NULL::integer,
        NULL::text,NULL::text,NULL::text,NULL::text,NULL::text,
        NULL::timestamptz,NULL::timestamptz,NULL::text,NULL::integer,
        NULL::text,'{}'::jsonb,NULL::uuid,replayed.outcome,
        replayed.reserved_jobs_in_window,replayed.consecutive_rejections,
        'replayed'::text;
      RETURN;
    END IF;
    RETURN QUERY
    SELECT
      job.job_id,job.evidence_id,
      (claim_event.details->>'lease_token')::uuid,job.status::text,
      job.route,job.attempts,job.evidence_content_sha256,
      evidence.kind::text,evidence.source_system,evidence.external_id,
      evidence.content,evidence.observed_at,evidence.recorded_at,
      evidence.sensitivity::text,job.checkpoint_sequence,
      job.checkpoint_sha256,job.result,replayed.event_id,replayed.outcome,
      replayed.reserved_jobs_in_window,replayed.consecutive_rejections,
      'replayed'::text
    FROM memory.evidence_extraction_job AS job
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=job.owner_user_id
     AND evidence.evidence_id=job.evidence_id
    JOIN memory.evidence_extraction_event AS claim_event
      ON claim_event.owner_user_id=job.owner_user_id
     AND claim_event.job_id=job.job_id
     AND claim_event.event_type='claimed'
     AND claim_event.operation_id=p_operation_id
    WHERE job.owner_user_id=actor AND job.job_id=replayed.job_id
      AND job.job_id=p_expected_job_id
      AND job.evidence_content_sha256=p_expected_content_sha256;
    RETURN;
  END IF;

  SELECT count(*)::integer INTO reserved_count
  FROM memory.v5_local_inference_event AS event
  WHERE event.owner_user_id=actor
    AND event.action='reserved'
    AND event.created_at>=
      clock_timestamp()-make_interval(secs=>p_rolling_window_seconds);

  SELECT count(*)::integer,coalesce(bool_and(recent.outcome='rejected'),false)
  INTO latest_count,latest_all_rejected
  FROM (
    SELECT event.outcome
    FROM memory.v5_local_inference_event AS event
    WHERE event.owner_user_id=actor AND event.action='completed'
    ORDER BY event.created_at DESC,event.event_id DESC
    LIMIT p_failure_threshold
  ) AS recent;
  rejection_count := CASE
    WHEN latest_count=p_failure_threshold AND latest_all_rejected
      THEN p_failure_threshold ELSE 0
  END;
  IF reserved_count>=p_max_reserved_jobs THEN
    block_reason := 'quota_exhausted';
  ELSIF rejection_count>=p_failure_threshold THEN
    block_reason := 'circuit_open';
  END IF;
  IF block_reason IS NOT NULL THEN
    INSERT INTO memory.v5_local_inference_event(
      event_id,owner_user_id,operation_id,run_id,action,outcome,
      target_job_sha256,target_content_sha256,
      provider_id,provider_version,provider_model_sha256,model_file_sha256,
      runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
      local_model_calls,external_model_calls,rejection_code,
      rolling_window_seconds,max_reserved_jobs,failure_threshold,
      reserved_jobs_in_window,consecutive_rejections
    ) VALUES (
      reservation_id,actor,p_operation_id,p_run_id,'blocked',block_reason,
      target_job_sha,p_expected_content_sha256,
      p_provider_id,p_provider_version,p_provider_model_sha256,
      p_model_file_sha256,p_runtime_revision_sha256,
      p_policy_compiler_sha256,worker_sha,0,0,block_reason,
      p_rolling_window_seconds,p_max_reserved_jobs,p_failure_threshold,
      reserved_count,rejection_count
    );
    RETURN QUERY SELECT
      NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,p_route,NULL::integer,
      NULL::text,NULL::text,NULL::text,NULL::text,NULL::text,
      NULL::timestamptz,NULL::timestamptz,NULL::text,NULL::integer,
      NULL::text,'{}'::jsonb,NULL::uuid,block_reason,reserved_count,
      rejection_count,'applied'::text;
    RETURN;
  END IF;

  SELECT * INTO claimed
  FROM memory.claim_owner_evidence_extraction_job_v1(
    p_operation_id,p_route,p_worker_id,p_lease_seconds,p_max_attempts
  );
  IF NOT FOUND THEN
    RETURN;
  END IF;
  IF claimed.job_id<>p_expected_job_id
     OR claimed.evidence_content_sha256<>p_expected_content_sha256 THEN
    RAISE EXCEPTION 'V5 local inference target changed before claim'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_local_inference_event(
    event_id,owner_user_id,operation_id,run_id,job_id,action,outcome,
    target_job_sha256,target_content_sha256,
    provider_id,provider_version,provider_model_sha256,model_file_sha256,
    runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
    local_model_calls,external_model_calls,rolling_window_seconds,
    max_reserved_jobs,failure_threshold,reserved_jobs_in_window,
    consecutive_rejections
  ) VALUES (
    reservation_id,actor,p_operation_id,p_run_id,claimed.job_id,
    'reserved','reserved',target_job_sha,p_expected_content_sha256,
    p_provider_id,p_provider_version,
    p_provider_model_sha256,p_model_file_sha256,p_runtime_revision_sha256,
    p_policy_compiler_sha256,worker_sha,0,0,p_rolling_window_seconds,
    p_max_reserved_jobs,p_failure_threshold,reserved_count+1,rejection_count
  );

  RETURN QUERY SELECT
    claimed.job_id,claimed.evidence_id,claimed.lease_token,
    claimed.status,claimed.route,claimed.attempts,
    claimed.evidence_content_sha256,claimed.evidence_kind,
    claimed.evidence_source_system,claimed.evidence_external_id,
    claimed.evidence_content,claimed.evidence_observed_at,
    claimed.evidence_recorded_at,claimed.evidence_sensitivity,
    claimed.checkpoint_sequence,claimed.checkpoint_sha256,claimed.result,
    reservation_id,'reserved'::text,reserved_count+1,rejection_count,
    'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION memory.persist_owner_v5_local_packet_v1(
  p_operation_id uuid,
  p_packet_id uuid,
  p_job_id uuid,
  p_lease_token uuid,
  p_worker_id text,
  p_expected_content_sha256 text,
  p_provider_version text,
  p_provider_model_sha256 text,
  p_model_file_sha256 text,
  p_runtime_revision_sha256 text,
  p_policy_compiler_sha256 text,
  p_provider_output_sha256 text,
  p_normalized_packet_sha256 text,
  p_normalized_packet jsonb,
  p_manual_review_required boolean,
  p_local_model_calls integer
)
RETURNS TABLE(
  packet_id uuid,
  job_id uuid,
  status text,
  validator_packet_sha256 text,
  packet_storage_sha256 text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  calculated_storage_sha256 text;
  current_job memory.evidence_extraction_job%ROWTYPE;
  evidence_record memory.evidence%ROWTYPE;
  replayed memory.evidence_extraction_packet_v5_local%ROWTYPE;
  entity_count integer;
  observation_count integer;
  comparison_count integer;
  deferral_count integer;
  final_summary jsonb;
  next_result jsonb;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5 local packet persistence requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_packet_id IS NULL OR p_job_id IS NULL
     OR p_lease_token IS NULL OR p_worker_id IS NULL
     OR btrim(p_worker_id)='' OR length(p_worker_id)>500
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_provider_version !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
     OR p_provider_model_sha256 !~ '^[0-9a-f]{64}$'
     OR p_model_file_sha256 !~ '^[0-9a-f]{64}$'
     OR p_runtime_revision_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_compiler_sha256 !~ '^[0-9a-f]{64}$'
     OR p_provider_output_sha256 !~ '^[0-9a-f]{64}$'
     OR p_normalized_packet_sha256 !~ '^[0-9a-f]{64}$'
     OR p_normalized_packet IS NULL
     OR jsonb_typeof(p_normalized_packet)<>'object'
     OR pg_column_size(p_normalized_packet)>262144
     OR p_manual_review_required IS NULL
     OR p_local_model_calls NOT BETWEEN 0 AND 1 THEN
    RAISE EXCEPTION 'V5 local packet persistence inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  calculated_storage_sha256 := encode(
    public.digest(convert_to(p_normalized_packet::text,'UTF8'),'sha256'),'hex'
  );
  IF p_normalized_packet->>'contract_version'
       IS DISTINCT FROM 'memory_v1_relational_extraction_v5'
     OR p_normalized_packet->>'predicate_registry_version'
       IS DISTINCT FROM 'memory_predicate_registry_v5'
     OR p_normalized_packet @? '$.**.owner_user_id'
     OR p_normalized_packet @? '$.**.vantage_id'
     OR jsonb_typeof(p_normalized_packet->'entity_mentions')<>'array'
     OR jsonb_typeof(p_normalized_packet->'observations')<>'array'
     OR jsonb_typeof(p_normalized_packet->'comparison_hints')<>'array'
     OR jsonb_typeof(p_normalized_packet->'deferrals')<>'array'
     OR jsonb_typeof(p_normalized_packet->'packet_findings')<>'array' THEN
    RAISE EXCEPTION 'normalized local V5 packet contract is invalid'
      USING ERRCODE='23514';
  END IF;

  entity_count := jsonb_array_length(p_normalized_packet->'entity_mentions');
  observation_count := jsonb_array_length(p_normalized_packet->'observations');
  comparison_count := jsonb_array_length(p_normalized_packet->'comparison_hints');
  deferral_count := jsonb_array_length(p_normalized_packet->'deferrals');
  IF entity_count>24 OR observation_count>32
     OR comparison_count>32 OR deferral_count>32 THEN
    RAISE EXCEPTION 'normalized local V5 packet exceeds bounded counts'
      USING ERRCODE='23514';
  END IF;
  IF (
       SELECT count(DISTINCT item->>'entity_ref')
       FROM jsonb_array_elements(p_normalized_packet->'entity_mentions') AS item
     )<>entity_count
     OR (
       SELECT count(DISTINCT item->>'observation_ref')
       FROM jsonb_array_elements(p_normalized_packet->'observations') AS item
     )<>observation_count
     OR EXISTS (
       SELECT 1
       FROM jsonb_array_elements(p_normalized_packet->'observations') AS obs
       WHERE NOT EXISTS (
         SELECT 1
         FROM jsonb_array_elements(p_normalized_packet->'entity_mentions') AS ent
         WHERE ent->>'entity_ref'=obs->>'subject_entity_ref'
       ) OR (
         obs#>>'{object,kind}'='entity' AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(p_normalized_packet->'entity_mentions') AS ent
           WHERE ent->>'entity_ref'=obs#>>'{object,entity_ref}'
         )
       )
     ) THEN
    RAISE EXCEPTION 'normalized local V5 packet references are invalid'
      USING ERRCODE='23514';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM jsonb_array_elements(p_normalized_packet->'observations') AS obs
    WHERE obs->>'projection_class'='project_knowledge'
      AND obs#>>'{project_scope,state}'<>'unresolved'
  ) THEN
    RAISE EXCEPTION 'local V5 project knowledge must remain unresolved'
      USING ERRCODE='23514';
  END IF;

  SELECT packet.* INTO replayed
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id=actor
    AND (packet.operation_id=p_operation_id OR packet.job_id=p_job_id)
  ORDER BY (packet.operation_id=p_operation_id) DESC
  LIMIT 1;
  IF FOUND THEN
    IF replayed.packet_id<>p_packet_id OR replayed.job_id<>p_job_id
       OR replayed.evidence_content_sha256<>p_expected_content_sha256
       OR replayed.provider_version<>p_provider_version
       OR replayed.provider_model_sha256<>p_provider_model_sha256
       OR replayed.model_file_sha256<>p_model_file_sha256
       OR replayed.runtime_revision_sha256<>p_runtime_revision_sha256
       OR replayed.policy_compiler_sha256<>p_policy_compiler_sha256
       OR replayed.provider_output_sha256<>p_provider_output_sha256
       OR replayed.validator_packet_sha256<>p_normalized_packet_sha256
       OR replayed.packet_storage_sha256<>calculated_storage_sha256
       OR replayed.normalized_packet<>p_normalized_packet
       OR replayed.local_model_calls<>p_local_model_calls THEN
      RAISE EXCEPTION 'V5 local packet replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.packet_id,replayed.job_id,
      'review_required'::text,replayed.validator_packet_sha256,
      replayed.packet_storage_sha256,'replayed'::text;
    RETURN;
  END IF;

  SELECT job.* INTO current_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND OR current_job.status<>'processing'
     OR current_job.route<>'relational_extraction'
     OR current_job.lease_token<>p_lease_token
     OR current_job.worker_id IS DISTINCT FROM p_worker_id
     OR current_job.lease_expires_at<=clock_timestamp()
     OR current_job.evidence_content_sha256
        IS DISTINCT FROM p_expected_content_sha256 THEN
    RAISE EXCEPTION 'V5 local packet lease or content binding is invalid'
      USING ERRCODE='23514';
  END IF;

  SELECT evidence.* INTO evidence_record
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=current_job.evidence_id;
  IF NOT FOUND OR evidence_record.source_system<>'public.chat_log'
     OR evidence_record.content_sha256<>p_expected_content_sha256
     OR p_normalized_packet#>>'{source_envelope,job_id}'
        IS DISTINCT FROM p_job_id::text
     OR p_normalized_packet#>>'{source_envelope,source_system}'
        IS DISTINCT FROM evidence_record.source_system
     OR p_normalized_packet#>>'{source_envelope,source_external_id}'
        IS DISTINCT FROM (CASE
          WHEN evidence_record.external_id ~*
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN lower(evidence_record.external_id)
          ELSE evidence_record.evidence_id::text
        END)
     OR p_normalized_packet#>>'{source_envelope,source_sha256}'
        IS DISTINCT FROM evidence_record.content_sha256
     OR (p_normalized_packet#>>'{source_envelope,source_recorded_at}')::timestamptz
        IS DISTINCT FROM evidence_record.recorded_at THEN
    RAISE EXCEPTION 'normalized local V5 packet source binding is invalid'
      USING ERRCODE='23514';
  END IF;

  final_summary := jsonb_build_object(
    'contract_version','memory_v1_evidence_extraction_packet_summary_v5_local',
    'packet_id',p_packet_id,'provider_id','local_llama_cpp',
    'provider_version',p_provider_version,
    'provider_model_sha256',p_provider_model_sha256,
    'model_file_sha256',p_model_file_sha256,
    'runtime_revision_sha256',p_runtime_revision_sha256,
    'policy_compiler_sha256',p_policy_compiler_sha256,
    'provider_output_sha256',p_provider_output_sha256,
    'validator_packet_sha256',p_normalized_packet_sha256,
    'packet_storage_sha256',calculated_storage_sha256,
    'manual_review_required',p_manual_review_required,
    'local_model_calls',p_local_model_calls,'external_model_calls',0,
    'write_counts',jsonb_build_object(
      'candidates',0,'claims',0,'staging',0,'qdrant',0,'prompt_influence',0
    )
  );
  next_result := jsonb_set(
    current_job.result,'{final}',jsonb_build_object(
      'status','review_required','sha256',calculated_storage_sha256,
      'payload',final_summary
    ),true
  );
  IF pg_column_size(next_result)>32768 THEN
    RAISE EXCEPTION 'sanitized local V5 job summary exceeds queue budget'
      USING ERRCODE='22023';
  END IF;

  INSERT INTO memory.evidence_extraction_packet_v5_local(
    packet_id,owner_user_id,operation_id,job_id,evidence_id,
    evidence_content_sha256,provider_id,provider_version,
    provider_model_sha256,model_file_sha256,runtime_revision_sha256,
    policy_compiler_sha256,provider_output_sha256,
    validator_packet_sha256,packet_storage_sha256,normalized_packet,
    manual_review_required,local_model_calls,external_model_calls,
    entity_mention_count,observation_count,comparison_hint_count,deferral_count
  ) VALUES (
    p_packet_id,actor,p_operation_id,p_job_id,current_job.evidence_id,
    p_expected_content_sha256,'local_llama_cpp',p_provider_version,
    p_provider_model_sha256,p_model_file_sha256,p_runtime_revision_sha256,
    p_policy_compiler_sha256,p_provider_output_sha256,
    p_normalized_packet_sha256,calculated_storage_sha256,p_normalized_packet,
    p_manual_review_required,p_local_model_calls,0,
    entity_count,observation_count,comparison_count,deferral_count
  );

  UPDATE memory.evidence_extraction_job AS job
  SET status='review_required',lease_token=NULL,lease_expires_at=NULL,
      last_error=NULL,result=next_result
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id;

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,from_status,to_status,
    actor_type,actor_ref,details
  ) VALUES (
    actor,p_job_id,p_operation_id,'review_required','processing',
    'review_required','worker',p_worker_id,
    jsonb_build_object(
      'packet_id',p_packet_id,
      'validator_packet_sha256',p_normalized_packet_sha256,
      'packet_storage_sha256',calculated_storage_sha256,
      'provider_output_sha256',p_provider_output_sha256,
      'provider_model_sha256',p_provider_model_sha256,
      'local_model_calls',p_local_model_calls,'external_model_calls',0
    )
  );

  RETURN QUERY SELECT p_packet_id,p_job_id,'review_required'::text,
    p_normalized_packet_sha256,calculated_storage_sha256,'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION memory.complete_owner_v5_local_inference_v1(
  p_operation_id uuid,
  p_reservation_event_id uuid,
  p_run_id uuid,
  p_job_id uuid,
  p_outcome text,
  p_local_model_calls integer,
  p_rejection_code text,
  p_provider_output_sha256 text,
  p_validator_packet_sha256 text,
  p_packet_storage_sha256 text
)
RETURNS TABLE(
  event_id uuid,
  outcome text,
  local_model_calls integer,
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
  reservation memory.v5_local_inference_event%ROWTYPE;
  replayed memory.v5_local_inference_event%ROWTYPE;
  new_event_id uuid := gen_random_uuid();
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5 local inference completion requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_reservation_event_id IS NULL
     OR p_run_id IS NULL OR p_job_id IS NULL
     OR p_outcome NOT IN ('accepted','rejected')
     OR p_local_model_calls NOT BETWEEN 0 AND 1
     OR (p_outcome='accepted' AND (
       p_rejection_code IS NOT NULL
       OR p_provider_output_sha256 !~ '^[0-9a-f]{64}$'
       OR p_validator_packet_sha256 !~ '^[0-9a-f]{64}$'
       OR p_packet_storage_sha256 !~ '^[0-9a-f]{64}$'))
     OR (p_outcome='rejected' AND (
       p_rejection_code !~ '^[a-z][a-z0-9_]{1,99}$'
       OR p_provider_output_sha256 IS NOT NULL
       OR p_validator_packet_sha256 IS NOT NULL
       OR p_packet_storage_sha256 IS NOT NULL)) THEN
    RAISE EXCEPTION 'V5 local inference completion inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT event.* INTO replayed
  FROM memory.v5_local_inference_event AS event
  WHERE event.owner_user_id=actor AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.action<>'completed'
       OR replayed.reservation_event_id<>p_reservation_event_id
       OR replayed.run_id<>p_run_id OR replayed.job_id<>p_job_id
       OR replayed.outcome<>p_outcome
       OR replayed.local_model_calls<>p_local_model_calls
       OR replayed.rejection_code IS DISTINCT FROM p_rejection_code
       OR replayed.provider_output_sha256
          IS DISTINCT FROM p_provider_output_sha256
       OR replayed.validator_packet_sha256
          IS DISTINCT FROM p_validator_packet_sha256
       OR replayed.packet_storage_sha256
          IS DISTINCT FROM p_packet_storage_sha256 THEN
      RAISE EXCEPTION 'V5 local inference completion replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.event_id,replayed.outcome,
      replayed.local_model_calls::integer,0,'replayed'::text;
    RETURN;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_v5_local_completion',actor::text,
      p_reservation_event_id::text),0
  ));
  SELECT event.* INTO reservation
  FROM memory.v5_local_inference_event AS event
  WHERE event.owner_user_id=actor
    AND event.event_id=p_reservation_event_id
    AND event.action='reserved' AND event.run_id=p_run_id
    AND event.job_id=p_job_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'V5 local inference reservation is absent or mismatched'
      USING ERRCODE='23514';
  END IF;
  IF p_outcome='accepted' AND NOT EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_packet_v5_local AS packet
    JOIN memory.evidence_extraction_job AS job
      ON job.owner_user_id=packet.owner_user_id AND job.job_id=packet.job_id
    WHERE packet.owner_user_id=actor AND packet.job_id=p_job_id
      AND packet.local_model_calls=p_local_model_calls
      AND packet.external_model_calls=0
      AND packet.provider_output_sha256=p_provider_output_sha256
      AND packet.validator_packet_sha256=p_validator_packet_sha256
      AND packet.packet_storage_sha256=p_packet_storage_sha256
      AND job.status='review_required'
  ) THEN
    RAISE EXCEPTION 'accepted local inference lacks its durable packet'
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
    consecutive_rejections
  ) VALUES (
    new_event_id,actor,p_operation_id,p_run_id,p_job_id,
    reservation.target_job_sha256,reservation.target_content_sha256,
    p_reservation_event_id,'completed',p_outcome,reservation.provider_id,
    reservation.provider_version,reservation.provider_model_sha256,
    reservation.model_file_sha256,reservation.runtime_revision_sha256,
    reservation.policy_compiler_sha256,reservation.worker_id_sha256,
    p_local_model_calls,0,p_rejection_code,p_provider_output_sha256,
    p_validator_packet_sha256,p_packet_storage_sha256,
    reservation.rolling_window_seconds,reservation.max_reserved_jobs,
    reservation.failure_threshold,reservation.reserved_jobs_in_window,
    CASE WHEN p_outcome='rejected'
      THEN least(reservation.consecutive_rejections+1,10) ELSE 0 END
  );
  RETURN QUERY SELECT new_event_id,p_outcome,p_local_model_calls,0,
    'applied'::text;
END
$function$;

GRANT USAGE ON SCHEMA memory TO memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION public.digest(bytea,text)
  TO memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.claim_owner_evidence_extraction_job_v1(
  uuid,text,text,integer,integer
) TO memory_v5_local_inference_maintainer;
GRANT SELECT,UPDATE ON memory.evidence_extraction_job
  TO memory_v5_local_inference_maintainer;
GRANT SELECT,INSERT ON memory.evidence_extraction_event
  TO memory_v5_local_inference_maintainer;
GRANT SELECT ON memory.evidence TO memory_v5_local_inference_maintainer;
GRANT SELECT,INSERT ON memory.v5_local_inference_event
  TO memory_v5_local_inference_maintainer;
GRANT SELECT,INSERT ON memory.evidence_extraction_packet_v5_local
  TO memory_v5_local_inference_maintainer;

ALTER FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) OWNER TO memory_v5_local_inference_maintainer;
ALTER FUNCTION memory.persist_owner_v5_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) OWNER TO memory_v5_local_inference_maintainer;
ALTER FUNCTION memory.complete_owner_v5_local_inference_v1(
  uuid,uuid,uuid,uuid,text,integer,text,text,text,text
) OWNER TO memory_v5_local_inference_maintainer;

REVOKE ALL ON TABLE memory.v5_local_inference_event,
  memory.evidence_extraction_packet_v5_local FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.guard_v5_local_inference_append_only()
  FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.persist_owner_v5_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.complete_owner_v5_local_inference_v1(
  uuid,uuid,uuid,uuid,text,integer,text,text,text,text
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;

GRANT EXECUTE ON FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.persist_owner_v5_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.complete_owner_v5_local_inference_v1(
  uuid,uuid,uuid,uuid,text,integer,text,text,text,text
) TO brains_app;

COMMIT;
