BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '180s';

DO $preflight$
BEGIN
  IF session_user <> 'sage'
     OR to_regrole('brains_app') IS NULL
     OR to_regrole('memory_v5_local_supersession_maintainer') IS NULL
     OR to_regrole('memory_v5_2_local_router_maintainer') IS NULL
     OR to_regclass('memory.v5_local_packet_supersession') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.v5_2_local_packet_route_event') IS NULL
     OR to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure(
       'memory.authoritative_owner_v5_2_packet_id_v1(uuid)'
     ) IS NULL THEN
    RAISE EXCEPTION 'duplicate packet authority prerequisites are absent';
  END IF;
END
$preflight$;

ALTER TABLE memory.v5_local_packet_supersession
  DROP CONSTRAINT v5_local_packet_supersession_reason_code_check;
ALTER TABLE memory.v5_local_packet_supersession
  ADD CONSTRAINT v5_local_packet_supersession_reason_code_check
  CHECK (
    reason_code IN (
      'temporal_persistence_matrix_reextracted',
      'pet_identity_semantics_reextracted',
      'duplicate_active_packet_reconciled'
    )
  );

GRANT SELECT ON memory.v5_2_local_packet_route_event
  TO memory_v5_local_supersession_maintainer;
GRANT SELECT ON memory.v5_local_packet_supersession
  TO memory_v5_2_local_router_maintainer;

CREATE OR REPLACE FUNCTION
memory.plan_owner_v5_2_duplicate_packet_supersession_v1(
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
  reason_code text,
  prior_created_at timestamptz,
  replacement_created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'duplicate packet plan requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_prior_packet_id IS NULL OR p_replacement_packet_id IS NULL
     OR p_prior_packet_id = p_replacement_packet_id THEN
    RAISE EXCEPTION 'duplicate packet ids are invalid'
      USING ERRCODE = '22023';
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
    'duplicate_active_packet_reconciled'::text,
    prior.created_at,
    replacement.created_at
  FROM memory.evidence_extraction_packet_v5_local AS prior
  JOIN memory.evidence_extraction_packet_v5_local AS replacement
    ON replacement.owner_user_id = prior.owner_user_id
   AND replacement.evidence_id = prior.evidence_id
   AND replacement.evidence_content_sha256 =
       prior.evidence_content_sha256
  JOIN memory.evidence_extraction_job AS prior_job
    ON prior_job.owner_user_id = prior.owner_user_id
   AND prior_job.job_id = prior.job_id
   AND prior_job.evidence_id = prior.evidence_id
  JOIN memory.evidence_extraction_job AS replacement_job
    ON replacement_job.owner_user_id = replacement.owner_user_id
   AND replacement_job.job_id = replacement.job_id
   AND replacement_job.evidence_id = replacement.evidence_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = prior.owner_user_id
   AND evidence.evidence_id = prior.evidence_id
  WHERE prior.owner_user_id = actor
    AND prior.packet_id = p_prior_packet_id
    AND replacement.packet_id = p_replacement_packet_id
    AND prior.created_at < replacement.created_at
    AND prior.provider_id = 'local_llama_cpp'
    AND replacement.provider_id = prior.provider_id
    AND prior.policy_compiler_sha256 =
        replacement.policy_compiler_sha256
    AND prior.normalized_packet->>'contract_version' =
        'memory_v1_relational_extraction_v5_2'
    AND replacement.normalized_packet->>'contract_version' =
        prior.normalized_packet->>'contract_version'
    AND prior.normalized_packet->>'predicate_registry_version' =
        'memory_predicate_registry_v5_2'
    AND replacement.normalized_packet->>'predicate_registry_version' =
        prior.normalized_packet->>'predicate_registry_version'
    AND prior.local_model_calls BETWEEN 0 AND 1
    AND replacement.local_model_calls BETWEEN 0 AND 1
    AND prior.external_model_calls = 0
    AND replacement.external_model_calls = 0
    AND prior.packet_storage_sha256 = encode(public.digest(convert_to(
      prior.normalized_packet::text, 'UTF8'
    ), 'sha256'), 'hex')
    AND replacement.packet_storage_sha256 = encode(public.digest(convert_to(
      replacement.normalized_packet::text, 'UTF8'
    ), 'sha256'), 'hex')
    AND prior_job.status::text = 'review_required'
    AND replacement_job.status::text = 'review_required'
    AND prior_job.route = 'relational_extraction'
    AND replacement_job.route = 'relational_extraction'
    AND prior_job.lease_token IS NULL
    AND replacement_job.lease_token IS NULL
    AND prior_job.lease_expires_at IS NULL
    AND replacement_job.lease_expires_at IS NULL
    AND prior_job.last_error IS NULL
    AND replacement_job.last_error IS NULL
    AND evidence.status::text = 'active'
    AND evidence.content_sha256 = prior.evidence_content_sha256
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_packet_supersession AS value
      WHERE value.owner_user_id = actor
        AND (
          value.prior_packet_id IN (
            prior.packet_id, replacement.packet_id
          )
          OR value.replacement_packet_id = prior.packet_id
        )
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_2_local_packet_route_event AS value
      WHERE value.owner_user_id = actor
        AND value.packet_id IN (
          prior.packet_id, replacement.packet_id
        )
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_packet_disposition AS value
      WHERE value.owner_user_id = actor
        AND value.packet_id IN (
          prior.packet_id, replacement.packet_id
        )
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.relational_stage_batch AS value
      WHERE value.owner_user_id = actor
        AND value.evidence_id = prior.evidence_id
    );
END
$function$;

CREATE OR REPLACE FUNCTION
memory.finalize_owner_v5_2_duplicate_packet_supersession_v1(
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
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
  planned record;
  replayed memory.v5_local_packet_supersession%ROWTYPE;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'duplicate packet apply requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_operation_id IS NULL OR p_supersession_id IS NULL
     OR p_prior_packet_id IS NULL OR p_replacement_packet_id IS NULL
     OR p_prior_packet_id = p_replacement_packet_id
     OR p_expected_prior_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_replacement_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_reason_code <> 'duplicate_active_packet_reconciled' THEN
    RAISE EXCEPTION 'duplicate packet apply inputs are invalid'
      USING ERRCODE = '22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws(
    '|', 'memory_v1_v5_2_duplicate_packet_supersession',
    actor::text, p_prior_packet_id::text
  ), 0));

  SELECT value.* INTO replayed
  FROM memory.v5_local_packet_supersession AS value
  WHERE value.owner_user_id = actor
    AND (
      value.operation_id = p_operation_id
      OR value.prior_packet_id = p_prior_packet_id
    );
  IF FOUND THEN
    IF replayed.operation_id <> p_operation_id
       OR replayed.supersession_id <> p_supersession_id
       OR replayed.prior_packet_id <> p_prior_packet_id
       OR replayed.replacement_packet_id <> p_replacement_packet_id
       OR replayed.prior_packet_storage_sha256 <>
          p_expected_prior_storage_sha256
       OR replayed.replacement_packet_storage_sha256 <>
          p_expected_replacement_storage_sha256
       OR replayed.reason_code <> p_reason_code THEN
      RAISE EXCEPTION 'duplicate packet replay conflicts'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY
    SELECT
      replayed.supersession_id,
      replayed.prior_packet_id,
      replayed.replacement_packet_id,
      replayed.reason_code,
      'replayed'::text;
    RETURN;
  END IF;

  SELECT * INTO planned
  FROM memory.plan_owner_v5_2_duplicate_packet_supersession_v1(
    p_prior_packet_id, p_replacement_packet_id
  );
  IF NOT FOUND
     OR planned.prior_packet_storage_sha256 <>
        p_expected_prior_storage_sha256
     OR planned.replacement_packet_storage_sha256 <>
        p_expected_replacement_storage_sha256
     OR planned.reason_code <> p_reason_code THEN
    RAISE EXCEPTION 'duplicate packet plan changed'
      USING ERRCODE = '23514';
  END IF;

  INSERT INTO memory.v5_local_packet_supersession(
    supersession_id,
    owner_user_id,
    operation_id,
    prior_packet_id,
    replacement_packet_id,
    evidence_id,
    reason_code,
    prior_packet_storage_sha256,
    replacement_packet_storage_sha256,
    prior_policy_compiler_sha256,
    replacement_policy_compiler_sha256
  ) VALUES (
    p_supersession_id,
    actor,
    p_operation_id,
    p_prior_packet_id,
    p_replacement_packet_id,
    planned.evidence_id,
    p_reason_code,
    planned.prior_packet_storage_sha256,
    planned.replacement_packet_storage_sha256,
    planned.prior_policy_compiler_sha256,
    planned.replacement_policy_compiler_sha256
  );

  RETURN QUERY
  SELECT
    p_supersession_id,
    p_prior_packet_id,
    p_replacement_packet_id,
    p_reason_code,
    'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.authoritative_owner_v5_2_packet_id_v1(
  p_evidence_id uuid
)
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
  WITH actor AS (
    SELECT memory.current_actor_user_id() AS owner_user_id
  ),
  candidates AS (
    SELECT
      packet.owner_user_id,
      packet.packet_id,
      packet.job_id,
      packet.evidence_id,
      packet.evidence_content_sha256
    FROM memory.evidence_extraction_packet_v5_local AS packet
    JOIN actor ON actor.owner_user_id = packet.owner_user_id
    JOIN memory.evidence_extraction_job AS job
      ON job.owner_user_id = packet.owner_user_id
     AND job.job_id = packet.job_id
     AND job.evidence_id = packet.evidence_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id = packet.owner_user_id
     AND evidence.evidence_id = packet.evidence_id
    WHERE actor.owner_user_id IS NOT NULL
      AND p_evidence_id IS NOT NULL
      AND packet.evidence_id = p_evidence_id
      AND packet.normalized_packet->>'contract_version' =
          'memory_v1_relational_extraction_v5_2'
      AND packet.normalized_packet->>'predicate_registry_version' =
          'memory_predicate_registry_v5_2'
      AND packet.provider_id = 'local_llama_cpp'
      AND packet.local_model_calls BETWEEN 0 AND 1
      AND packet.external_model_calls = 0
      AND packet.validator_packet_sha256 ~ '^[0-9a-f]{64}$'
      AND packet.packet_storage_sha256 = encode(public.digest(convert_to(
        packet.normalized_packet::text, 'UTF8'
      ), 'sha256'), 'hex')
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'entity_mentions') =
             'array'
        THEN jsonb_array_length(
          packet.normalized_packet->'entity_mentions'
        )
        ELSE -1
      END = packet.entity_mention_count
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'observations') =
             'array'
        THEN jsonb_array_length(packet.normalized_packet->'observations')
        ELSE -1
      END = packet.observation_count
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'comparison_hints') =
             'array'
        THEN jsonb_array_length(
          packet.normalized_packet->'comparison_hints'
        )
        ELSE -1
      END = packet.comparison_hint_count
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'deferrals') = 'array'
        THEN jsonb_array_length(packet.normalized_packet->'deferrals')
        ELSE -1
      END = packet.deferral_count
      AND job.status::text = 'review_required'
      AND job.route = 'relational_extraction'
      AND job.lease_token IS NULL
      AND job.lease_expires_at IS NULL
      AND job.last_error IS NULL
      AND evidence.status::text = 'active'
      AND evidence.content_sha256 = packet.evidence_content_sha256
  ),
  leaves AS (
    SELECT candidate.*
    FROM candidates AS candidate
    WHERE NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_event AS lineage
      JOIN memory.evidence_extraction_job AS successor_job
        ON successor_job.owner_user_id = lineage.owner_user_id
       AND successor_job.job_id = lineage.job_id
      WHERE lineage.owner_user_id = candidate.owner_user_id
        AND lineage.event_type = 'queued'
        AND lineage.details->>'prior_packet_id' =
            candidate.packet_id::text
        AND successor_job.evidence_id = candidate.evidence_id
        AND successor_job.evidence_content_sha256 =
            candidate.evidence_content_sha256
        AND successor_job.route = 'relational_extraction'
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_packet_supersession AS supersession
      WHERE supersession.owner_user_id = candidate.owner_user_id
        AND supersession.prior_packet_id = candidate.packet_id
    )
  ),
  authority AS (
    SELECT
      count(*) AS leaf_count,
      (array_agg(packet_id ORDER BY packet_id))[1] AS packet_id
    FROM leaves
  )
  SELECT CASE WHEN leaf_count = 1 THEN packet_id END
  FROM authority;
$function$;

GRANT EXECUTE ON FUNCTION
memory.authoritative_owner_v5_2_packet_id_v1(uuid)
  TO brains_app, memory_v5_local_supersession_maintainer;

ALTER FUNCTION
memory.plan_owner_v5_2_duplicate_packet_supersession_v1(uuid, uuid)
  OWNER TO memory_v5_local_supersession_maintainer;
ALTER FUNCTION
memory.finalize_owner_v5_2_duplicate_packet_supersession_v1(
  uuid, uuid, uuid, uuid, text, text, text
)
  OWNER TO memory_v5_local_supersession_maintainer;

REVOKE ALL ON FUNCTION
memory.plan_owner_v5_2_duplicate_packet_supersession_v1(uuid, uuid)
  FROM PUBLIC, brains_app, memory_v5_local_supersession_maintainer;
GRANT EXECUTE ON FUNCTION
memory.plan_owner_v5_2_duplicate_packet_supersession_v1(uuid, uuid)
  TO brains_app, memory_v5_local_supersession_maintainer;
REVOKE ALL ON FUNCTION
memory.finalize_owner_v5_2_duplicate_packet_supersession_v1(
  uuid, uuid, uuid, uuid, text, text, text
)
  FROM PUBLIC, brains_app, memory_v5_local_supersession_maintainer;
GRANT EXECUTE ON FUNCTION
memory.finalize_owner_v5_2_duplicate_packet_supersession_v1(
  uuid, uuid, uuid, uuid, text, text, text
)
  TO brains_app, memory_v5_local_supersession_maintainer;

COMMIT;
