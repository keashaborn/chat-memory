BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local packet supersession install requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regrole('memory_v5_local_disposition_maintainer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure(
       'memory.guard_v5_local_inference_append_only()'
     ) IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regclass('memory.v5_local_packet_review_artifact') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL THEN
    RAISE EXCEPTION 'local packet supersession prerequisites are absent';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_local_supersession_maintainer'
  ) THEN
    CREATE ROLE memory_v5_local_supersession_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.v5_local_packet_supersession (
  supersession_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  prior_packet_id uuid NOT NULL,
  replacement_packet_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  reason_code text NOT NULL
    CHECK (reason_code='temporal_persistence_matrix_reextracted'),
  prior_packet_storage_sha256 text NOT NULL
    CHECK (prior_packet_storage_sha256 ~ '^[0-9a-f]{64}$'),
  replacement_packet_storage_sha256 text NOT NULL
    CHECK (replacement_packet_storage_sha256 ~ '^[0-9a-f]{64}$'),
  prior_policy_compiler_sha256 text NOT NULL
    CHECK (prior_policy_compiler_sha256 ~ '^[0-9a-f]{64}$'),
  replacement_policy_compiler_sha256 text NOT NULL
    CHECK (replacement_policy_compiler_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (prior_packet_id<>replacement_packet_id),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,prior_packet_id),
  FOREIGN KEY(owner_user_id,prior_packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(
      owner_user_id,packet_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,replacement_packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(
      owner_user_id,packet_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS v5_local_packet_supersession_owner_time_idx
  ON memory.v5_local_packet_supersession(
    owner_user_id,created_at,supersession_id
  );

ALTER TABLE memory.v5_local_packet_supersession OWNER TO sage;
ALTER TABLE memory.v5_local_packet_supersession ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_packet_supersession FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.v5_local_packet_supersession;
CREATE POLICY owner_isolation ON memory.v5_local_packet_supersession
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());

DROP TRIGGER IF EXISTS v5_local_packet_supersession_append_only_guard
  ON memory.v5_local_packet_supersession;
CREATE TRIGGER v5_local_packet_supersession_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_packet_supersession
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_local_packet_supersession_v1(
  p_prior_packet_id uuid,
  p_replacement_packet_id uuid
)
RETURNS TABLE(
  prior_packet_id uuid,
  replacement_packet_id uuid,
  evidence_id uuid,
  prior_packet_storage_sha256 text,
  replacement_packet_storage_sha256 text,
  prior_policy_compiler_sha256 text,
  replacement_policy_compiler_sha256 text,
  prior_selector_version text,
  replacement_selector_version text,
  reason_code text,
  prior_observation_count integer,
  replacement_observation_count integer
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  compiler_v5 text := encode(public.digest(convert_to(
    to_jsonb('memory_v1_local_policy_compiler_v5'::text)::text,'UTF8'
  ),'sha256'),'hex');
  compiler_v6 text := encode(public.digest(convert_to(
    to_jsonb('memory_v1_local_policy_compiler_v6'::text)::text,'UTF8'
  ),'sha256'),'hex');
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local packet supersession plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_prior_packet_id IS NULL OR p_replacement_packet_id IS NULL
     OR p_prior_packet_id=p_replacement_packet_id THEN
    RAISE EXCEPTION 'local packet supersession ids are invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    prior.packet_id,
    replacement.packet_id,
    prior.evidence_id,
    prior.packet_storage_sha256,
    replacement.packet_storage_sha256,
    prior.policy_compiler_sha256,
    replacement.policy_compiler_sha256,
    prior_job.selector_version,
    replacement_job.selector_version,
    'temporal_persistence_matrix_reextracted'::text,
    prior.observation_count,
    replacement.observation_count
  FROM memory.evidence_extraction_packet_v5_local AS prior
  JOIN memory.evidence_extraction_packet_v5_local AS replacement
    ON replacement.owner_user_id=prior.owner_user_id
   AND replacement.evidence_id=prior.evidence_id
  JOIN memory.evidence_extraction_job AS prior_job
    ON prior_job.owner_user_id=prior.owner_user_id
   AND prior_job.job_id=prior.job_id
  JOIN memory.evidence_extraction_job AS replacement_job
    ON replacement_job.owner_user_id=replacement.owner_user_id
   AND replacement_job.job_id=replacement.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=prior.owner_user_id
   AND evidence.evidence_id=prior.evidence_id
  WHERE prior.owner_user_id=actor
    AND prior.packet_id=p_prior_packet_id
    AND replacement.packet_id=p_replacement_packet_id
    AND prior.evidence_content_sha256=replacement.evidence_content_sha256
    AND evidence.content_sha256=prior.evidence_content_sha256
    AND evidence.status='active'
    AND prior.provider_id='local_llama_cpp'
    AND replacement.provider_id='local_llama_cpp'
    AND prior.policy_compiler_sha256=compiler_v5
    AND replacement.policy_compiler_sha256=compiler_v6
    AND prior_job.selector_version=
      '20260719_v5_compound_guard_reextract_v1'
    AND replacement_job.selector_version=
      '20260719_v5_temporal_contract_reextract_v1'
    AND prior_job.status='review_required'
    AND replacement_job.status='review_required'
    AND prior_job.route='relational_extraction'
    AND replacement_job.route='relational_extraction'
    AND prior_job.lease_token IS NULL
    AND prior_job.lease_expires_at IS NULL
    AND prior_job.last_error IS NULL
    AND replacement_job.lease_token IS NULL
    AND replacement_job.lease_expires_at IS NULL
    AND replacement_job.last_error IS NULL
    AND prior.local_model_calls=1
    AND replacement.local_model_calls=1
    AND prior.external_model_calls=0
    AND replacement.external_model_calls=0
    AND prior.manual_review_required
    AND replacement.manual_review_required
    AND prior.entity_mention_count>0
    AND prior.observation_count>0
    AND replacement.entity_mention_count>0
    AND replacement.observation_count>0
    AND jsonb_typeof(prior.normalized_packet)='object'
    AND jsonb_typeof(replacement.normalized_packet)='object'
    AND jsonb_array_length(prior.normalized_packet->'observations')=
        prior.observation_count
    AND jsonb_array_length(replacement.normalized_packet->'observations')=
        replacement.observation_count
    AND prior.packet_storage_sha256=encode(public.digest(convert_to(
      prior.normalized_packet::text,'UTF8'
    ),'sha256'),'hex')
    AND replacement.packet_storage_sha256=encode(public.digest(convert_to(
      replacement.normalized_packet::text,'UTF8'
    ),'sha256'),'hex')
    AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(
        prior.normalized_packet->'observations'
      ) AS observation(value)
      WHERE observation.value#>>'{temporal,basis}'='calendar'
        AND observation.value#>>'{temporal,source_form}'='partial_absolute'
        AND observation.value#>>'{temporal,anchored_to_source_time}'='false'
        AND observation.value#>'{temporal,reason_codes}'
            ? 'explicit_day_and_year'
    )
    AND NOT EXISTS (
      SELECT 1
      FROM jsonb_array_elements(
        replacement.normalized_packet->'observations'
      ) AS observation(value)
      WHERE observation.value#>>'{temporal,basis}'='calendar'
        AND observation.value#>>'{temporal,source_form}'='partial_absolute'
        AND observation.value#>>'{temporal,anchored_to_source_time}'='false'
        AND observation.value#>'{temporal,reason_codes}'
            ? 'explicit_day_and_year'
    )
    AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(
        replacement.normalized_packet->'observations'
      ) AS observation(value)
      WHERE observation.value#>>'{temporal,basis}'='calendar'
        AND observation.value#>>'{temporal,source_form}'='absolute'
        AND observation.value#>>'{temporal,anchored_to_source_time}'='false'
        AND observation.value#>'{temporal,reason_codes}'
            ? 'explicit_day_and_year'
        AND observation.value#>'{temporal,reason_codes}'
            ? 'explicit_calendar_year_source_form'
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_supersession AS supersession
      WHERE supersession.owner_user_id=actor
        AND supersession.prior_packet_id=prior.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_disposition AS disposition
      WHERE disposition.owner_user_id=actor
        AND disposition.packet_id=prior.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_review_artifact AS artifact
      WHERE artifact.owner_user_id=actor
        AND artifact.packet_id=prior.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id=actor
        AND stage.evidence_id=prior.evidence_id
    );
END
$function$;

CREATE OR REPLACE FUNCTION memory.finalize_owner_v5_local_packet_supersession_v1(
  p_operation_id uuid,
  p_supersession_id uuid,
  p_prior_packet_id uuid,
  p_replacement_packet_id uuid,
  p_expected_prior_storage_sha256 text,
  p_expected_replacement_storage_sha256 text,
  p_reason_code text
)
RETURNS TABLE(
  supersession_id uuid,
  prior_packet_id uuid,
  replacement_packet_id uuid,
  reason_code text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  planned record;
  replayed memory.v5_local_packet_supersession%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local packet supersession apply requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_supersession_id IS NULL
     OR p_prior_packet_id IS NULL OR p_replacement_packet_id IS NULL
     OR p_prior_packet_id=p_replacement_packet_id
     OR p_expected_prior_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_replacement_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_reason_code<>'temporal_persistence_matrix_reextracted' THEN
    RAISE EXCEPTION 'local packet supersession inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_local_packet_supersession',actor::text,
    p_prior_packet_id::text),0));

  SELECT value.* INTO replayed
  FROM memory.v5_local_packet_supersession AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id
      OR value.prior_packet_id=p_prior_packet_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.supersession_id<>p_supersession_id
       OR replayed.prior_packet_id<>p_prior_packet_id
       OR replayed.replacement_packet_id<>p_replacement_packet_id
       OR replayed.prior_packet_storage_sha256<>
          p_expected_prior_storage_sha256
       OR replayed.replacement_packet_storage_sha256<>
          p_expected_replacement_storage_sha256
       OR replayed.reason_code<>p_reason_code THEN
      RAISE EXCEPTION 'local packet supersession replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.supersession_id,replayed.prior_packet_id,
      replayed.replacement_packet_id,replayed.reason_code,'replayed'::text;
    RETURN;
  END IF;

  SELECT * INTO planned
  FROM memory.plan_owner_v5_local_packet_supersession_v1(
    p_prior_packet_id,p_replacement_packet_id
  );
  IF NOT FOUND
     OR planned.prior_packet_storage_sha256<>
        p_expected_prior_storage_sha256
     OR planned.replacement_packet_storage_sha256<>
        p_expected_replacement_storage_sha256
     OR planned.reason_code<>p_reason_code THEN
    RAISE EXCEPTION 'local packet supersession plan changed'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_local_packet_supersession(
    supersession_id,owner_user_id,operation_id,prior_packet_id,
    replacement_packet_id,evidence_id,reason_code,
    prior_packet_storage_sha256,replacement_packet_storage_sha256,
    prior_policy_compiler_sha256,replacement_policy_compiler_sha256
  ) VALUES (
    p_supersession_id,actor,p_operation_id,p_prior_packet_id,
    p_replacement_packet_id,planned.evidence_id,p_reason_code,
    planned.prior_packet_storage_sha256,
    planned.replacement_packet_storage_sha256,
    planned.prior_policy_compiler_sha256,
    planned.replacement_policy_compiler_sha256
  );

  RETURN QUERY SELECT p_supersession_id,p_prior_packet_id,
    p_replacement_packet_id,p_reason_code,'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  packet_id uuid,
  job_id uuid,
  evidence_id uuid,
  packet_storage_sha256 text,
  disposition_route text,
  reason_code text,
  entity_mention_count integer,
  observation_count integer,
  comparison_hint_count integer,
  deferral_count integer,
  packet_created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local packet disposition plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'local packet disposition plan limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    packet.packet_id,
    packet.job_id,
    packet.evidence_id,
    packet.packet_storage_sha256,
    CASE WHEN packet.entity_mention_count=0
                   AND packet.observation_count=0
                   AND packet.comparison_hint_count=0
                   AND packet.deferral_count>0
                   AND NOT packet.manual_review_required
         THEN 'terminal_deferral' ELSE 'manual_review' END,
    CASE WHEN packet.entity_mention_count=0
                   AND packet.observation_count=0
                   AND packet.comparison_hint_count=0
                   AND packet.deferral_count>0
                   AND NOT packet.manual_review_required
         THEN 'deferral_only_no_stage' ELSE 'manual_review_required' END,
    packet.entity_mention_count,
    packet.observation_count,
    packet.comparison_hint_count,
    packet.deferral_count,
    packet.created_at
  FROM memory.evidence_extraction_packet_v5_local AS packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=packet.owner_user_id
   AND job.job_id=packet.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=packet.owner_user_id
   AND evidence.evidence_id=packet.evidence_id
  WHERE packet.owner_user_id=actor
    AND job.status='review_required'
    AND job.route='relational_extraction'
    AND job.lease_token IS NULL
    AND job.lease_expires_at IS NULL
    AND job.last_error IS NULL
    AND evidence.status='active'
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_disposition AS disposition
      WHERE disposition.owner_user_id=actor
        AND disposition.packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_supersession AS supersession
      WHERE supersession.owner_user_id=actor
        AND supersession.prior_packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_review_artifact AS artifact
      WHERE artifact.owner_user_id=actor
        AND artifact.packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id=actor
        AND stage.evidence_id=packet.evidence_id
    )
  ORDER BY
    CASE WHEN packet.entity_mention_count=0
                   AND packet.observation_count=0
                   AND packet.comparison_hint_count=0
                   AND packet.deferral_count>0
                   AND NOT packet.manual_review_required
         THEN 0 ELSE 1 END,
    packet.created_at,
    packet.packet_id
  LIMIT p_limit;
END
$function$;

GRANT USAGE ON SCHEMA memory TO memory_v5_local_supersession_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_local_supersession_maintainer;
GRANT EXECUTE ON FUNCTION public.digest(bytea,text)
  TO memory_v5_local_supersession_maintainer;
GRANT SELECT ON memory.evidence,memory.evidence_extraction_job,
  memory.evidence_extraction_packet_v5_local,
  memory.v5_local_packet_disposition,
  memory.v5_local_packet_review_artifact,
  memory.relational_stage_batch,
  memory.v5_local_packet_supersession
TO memory_v5_local_supersession_maintainer;
GRANT INSERT ON memory.v5_local_packet_supersession
  TO memory_v5_local_supersession_maintainer;
GRANT SELECT ON memory.v5_local_packet_supersession
  TO memory_v5_local_disposition_maintainer;

ALTER FUNCTION memory.plan_owner_v5_local_packet_supersession_v1(uuid,uuid)
  OWNER TO memory_v5_local_supersession_maintainer;
ALTER FUNCTION memory.finalize_owner_v5_local_packet_supersession_v1(
  uuid,uuid,uuid,uuid,text,text,text
) OWNER TO memory_v5_local_supersession_maintainer;
ALTER FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  OWNER TO memory_v5_local_disposition_maintainer;

REVOKE ALL ON memory.v5_local_packet_supersession FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_local_packet_supersession_v1(uuid,uuid)
  FROM PUBLIC,brains_app,memory_v5_local_supersession_maintainer;
REVOKE ALL ON FUNCTION
  memory.finalize_owner_v5_local_packet_supersession_v1(
    uuid,uuid,uuid,uuid,text,text,text
  ) FROM PUBLIC,brains_app,memory_v5_local_supersession_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_local_packet_supersession_v1(uuid,uuid)
  TO brains_app,memory_v5_local_supersession_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.finalize_owner_v5_local_packet_supersession_v1(
    uuid,uuid,uuid,uuid,text,text,text
  ) TO brains_app;

COMMIT;
