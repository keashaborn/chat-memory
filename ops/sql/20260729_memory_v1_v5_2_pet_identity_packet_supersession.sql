BEGIN;

ALTER TABLE memory.v5_local_packet_supersession
  DROP CONSTRAINT v5_local_packet_supersession_reason_code_check;
ALTER TABLE memory.v5_local_packet_supersession
  ADD CONSTRAINT v5_local_packet_supersession_reason_code_check
  CHECK (
    reason_code IN (
      'temporal_persistence_matrix_reextracted',
      'pet_identity_semantics_reextracted'
    )
  );

CREATE OR REPLACE FUNCTION
memory.plan_owner_v5_2_pet_identity_packet_supersession_v1(
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
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION
      'pet identity packet supersession plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_prior_packet_id IS NULL OR p_replacement_packet_id IS NULL
     OR p_prior_packet_id=p_replacement_packet_id THEN
    RAISE EXCEPTION 'pet identity packet supersession ids are invalid'
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
    'pet_identity_semantics_reextracted'::text,
    prior.observation_count,
    replacement.observation_count
  FROM memory.evidence_extraction_packet_v5_local AS prior
  JOIN memory.evidence_extraction_packet_v5_local AS replacement
    ON replacement.owner_user_id=prior.owner_user_id
   AND replacement.evidence_id=prior.evidence_id
   AND replacement.evidence_content_sha256=prior.evidence_content_sha256
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
    AND evidence.status='active'
    AND evidence.content_sha256=prior.evidence_content_sha256
    AND prior.provider_id='local_llama_cpp'
    AND replacement.provider_id='local_llama_cpp'
    AND prior_job.selector_version='20260729_v4_contextual_resplit'
    AND replacement_job.selector_version=
      '20260729_v5_2_pet_identity_compiler_reextract_v1'
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
    AND replacement.entity_mention_count=1
    AND replacement.observation_count=1
    AND jsonb_typeof(prior.normalized_packet)='object'
    AND jsonb_typeof(replacement.normalized_packet)='object'
    AND prior.packet_storage_sha256=encode(public.digest(convert_to(
      prior.normalized_packet::text,'UTF8'
    ),'sha256'),'hex')
    AND replacement.packet_storage_sha256=encode(public.digest(convert_to(
      replacement.normalized_packet::text,'UTF8'
    ),'sha256'),'hex')
    AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(
        replacement.normalized_packet->'observations'
      ) AS observation(value)
      WHERE observation.value->>'predicate'='identity.name'
    )
    AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(
        replacement.normalized_packet->'entity_mentions'
      ) AS entity(value)
      WHERE entity.value->>'entity_type'='animal'
        AND entity.value->>'mention_kind'='named'
        AND entity.value->>'relationship_role'='pet:reported'
    )
    AND NOT EXISTS (
      SELECT 1
      FROM jsonb_array_elements(
        replacement.normalized_packet->'observations'
      ) AS observation(value)
      WHERE observation.value->>'predicate'='life_event.died'
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_packet_supersession AS supersession
      WHERE supersession.owner_user_id=actor
        AND supersession.prior_packet_id=prior.packet_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id=actor
        AND stage.evidence_id=prior.evidence_id
    );
END
$function$;

CREATE OR REPLACE FUNCTION
memory.finalize_owner_v5_2_pet_identity_packet_supersession_v1(
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
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION
      'pet identity packet supersession apply requires brains_app session'
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
     OR p_reason_code<>'pet_identity_semantics_reextracted' THEN
    RAISE EXCEPTION 'pet identity packet supersession inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_2_pet_identity_packet_supersession',
    actor::text,p_prior_packet_id::text),0));

  SELECT value.* INTO replayed
  FROM memory.v5_local_packet_supersession AS value
  WHERE value.owner_user_id=actor
    AND (
      value.operation_id=p_operation_id
      OR value.prior_packet_id=p_prior_packet_id
    );
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
      RAISE EXCEPTION 'pet identity packet supersession replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.supersession_id,replayed.prior_packet_id,
      replayed.replacement_packet_id,replayed.reason_code,'replayed'::text;
    RETURN;
  END IF;

  SELECT * INTO planned
  FROM memory.plan_owner_v5_2_pet_identity_packet_supersession_v1(
    p_prior_packet_id,p_replacement_packet_id
  );
  IF NOT FOUND
     OR planned.prior_packet_storage_sha256<>
        p_expected_prior_storage_sha256
     OR planned.replacement_packet_storage_sha256<>
        p_expected_replacement_storage_sha256
     OR planned.reason_code<>p_reason_code THEN
    RAISE EXCEPTION 'pet identity packet supersession plan changed'
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

ALTER FUNCTION
memory.plan_owner_v5_2_pet_identity_packet_supersession_v1(uuid,uuid)
OWNER TO memory_v5_local_supersession_maintainer;
ALTER FUNCTION
memory.finalize_owner_v5_2_pet_identity_packet_supersession_v1(
  uuid,uuid,uuid,uuid,text,text,text
)
OWNER TO memory_v5_local_supersession_maintainer;

REVOKE ALL ON FUNCTION
memory.plan_owner_v5_2_pet_identity_packet_supersession_v1(uuid,uuid)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
memory.finalize_owner_v5_2_pet_identity_packet_supersession_v1(
  uuid,uuid,uuid,uuid,text,text,text
)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
memory.plan_owner_v5_2_pet_identity_packet_supersession_v1(uuid,uuid)
TO brains_app;
GRANT EXECUTE ON FUNCTION
memory.finalize_owner_v5_2_pet_identity_packet_supersession_v1(
  uuid,uuid,uuid,uuid,text,text,text
)
TO brains_app;

COMMIT;
