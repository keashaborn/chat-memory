BEGIN;

DO $prerequisite$
BEGIN
  IF to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure(
       'memory.v5_projection_semantic_key_sha256(uuid,memory.projection_lane_v5,uuid,text,text,uuid,text,memory.observation_polarity,memory.observation_modality,jsonb)'
     ) IS NULL
     OR to_regclass('memory.projection_plan') IS NULL THEN
    RAISE EXCEPTION 'projection preflight API prerequisites are missing';
  END IF;
END
$prerequisite$;

CREATE OR REPLACE FUNCTION memory.preflight_projection_source_v5(
  p_observation_id uuid
)
RETURNS TABLE(
  observation_id uuid,
  observation_sha256 text,
  evidence_id uuid,
  predicate text,
  predicate_registry_version text,
  polarity text,
  modality text,
  projection_class text,
  surface_policy text,
  extraction_confidence text,
  subject_entity_id uuid,
  object_entity_id uuid,
  subject_entity_type text,
  subject_entity_status text,
  object_entity_type text,
  object_entity_status text,
  object_canonical_name text,
  evidence_content_sha256 text,
  evidence_status text,
  evidence_observed_at timestamptz,
  temporal_semantic text,
  temporal_shape text,
  temporal_basis text,
  temporal_source_form text,
  temporal_certainty text,
  temporal_precision text,
  temporal_normalized_sha256 text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  matched integer;
BEGIN
  actor := memory.require_v5_writer_context();
  RETURN QUERY
  SELECT
    observation.observation_id,
    observation.observation_sha256,
    observation.evidence_id,
    observation.predicate,
    observation.predicate_registry_version,
    observation.polarity::text,
    observation.modality::text,
    observation.projection_class::text,
    observation.surface_policy::text,
    observation.extraction_confidence::text,
    binding.subject_entity_id,
    binding.object_entity_id,
    subject_entity.entity_type,
    subject_entity.status::text,
    object_entity.entity_type,
    object_entity.status::text,
    object_entity.canonical_name,
    evidence.content_sha256,
    evidence.status::text,
    evidence.observed_at,
    temporal.semantic::text,
    temporal.shape::text,
    temporal.basis::text,
    temporal.source_form::text,
    temporal.certainty::text,
    temporal.precision::text,
    temporal.normalized_sha256
  FROM memory.observation AS observation
  JOIN memory.observation_entity_binding AS binding
    ON binding.owner_user_id=observation.owner_user_id
   AND binding.observation_id=observation.observation_id
  JOIN memory.entity AS subject_entity
    ON subject_entity.owner_user_id=binding.owner_user_id
   AND subject_entity.entity_id=binding.subject_entity_id
  JOIN memory.entity AS object_entity
    ON object_entity.owner_user_id=binding.owner_user_id
   AND object_entity.entity_id=binding.object_entity_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
  JOIN memory.observation_temporal AS temporal
    ON temporal.owner_user_id=observation.owner_user_id
   AND temporal.observation_id=observation.observation_id
  WHERE observation.owner_user_id=actor
    AND observation.observation_id=p_observation_id
    AND evidence.status='active'
    AND subject_entity.status='active'
    AND object_entity.status='active';
  GET DIAGNOSTICS matched = ROW_COUNT;
  IF matched <> 1 THEN
    RAISE EXCEPTION 'complete owner-scoped projection source not found'
      USING ERRCODE = 'P0002';
  END IF;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_projection_packet_v5(
  p_plan_id uuid,
  p_packet_text text
)
RETURNS TABLE(
  packet_text_sha256 text,
  semantic_key_sha256 text,
  projection_sha256 text,
  packet_sha256 text,
  owner_manifest_sha256 text,
  existing_claims integer,
  existing_plans integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  packet jsonb;
  projection jsonb;
  identity jsonb;
  payload jsonb;
  observation_input jsonb;
  observation_value uuid;
  semantic_value text;
  projection_hash text;
  packet_hash text;
  owner_manifest text;
  claim_count integer;
  plan_count integer;
  source_count integer;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_plan_id IS NULL OR btrim(COALESCE(p_packet_text,''))='' THEN
    RAISE EXCEPTION 'projection preflight identifiers are required'
      USING ERRCODE = '22023';
  END IF;
  BEGIN
    packet := p_packet_text::jsonb;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'projection packet is not valid JSON'
      USING ERRCODE = '22023';
  END;
  IF NOT memory.v5_jsonb_exact_keys(packet,ARRAY[
       'contract_version','predicate_registry_version',
       'projection_policy_version','projector','projector_version',
       'projections','packet_sha256'
     ])
     OR packet->>'contract_version' <> 'memory_v1_projection_plan_v5'
     OR packet->>'predicate_registry_version'
          <> 'memory_predicate_registry_v5'
     OR packet->>'projection_policy_version'
          <> 'memory_projection_policy_v5'
     OR packet->>'projector' <> 'memory_v1_deterministic_projection_v5'
     OR packet->>'projector_version' <> 'occupation_claim_v1'
     OR jsonb_typeof(packet->'projections') <> 'array'
     OR jsonb_array_length(packet->'projections') <> 1 THEN
    RAISE EXCEPTION 'projection packet envelope is outside the supported contract'
      USING ERRCODE = '23514';
  END IF;
  projection := packet->'projections'->0;
  identity := projection->'identity';
  payload := projection->'payload';
  observation_input := projection->'observation_inputs'->0;
  IF NOT memory.v5_jsonb_exact_keys(projection,ARRAY[
       'projection_ref','lane','observation_inputs','identity','target',
       'temporal_policy','review','relations','payload'
     ])
     OR NOT memory.v5_jsonb_exact_keys(identity,ARRAY[
       'subject_entity_id','predicate','object_kind','object_entity_id',
       'object_literal_sha256','polarity','modality','semantic_key_sha256'
     ])
     OR NOT memory.v5_jsonb_exact_keys(payload,ARRAY[
       'kind','claim_class','canonical_text','surface_policy'
     ])
     OR NOT memory.v5_jsonb_exact_keys(observation_input,ARRAY[
       'observation_id','observation_sha256','stance'
     ])
     OR NOT memory.v5_jsonb_exact_keys(projection->'target',ARRAY[
       'action','aggregate_id','expected_revision_number','reason_codes'
     ])
     OR NOT memory.v5_jsonb_exact_keys(projection->'temporal_policy',ARRAY[
       'canonical_source','materialization','source_observation_id'
     ])
     OR NOT memory.v5_jsonb_exact_keys(projection->'review',ARRAY[
       'state','authorization_required','reason_codes'
     ])
     OR projection->>'projection_ref' <> 'p01'
     OR projection->>'lane' <> 'claim'
     OR jsonb_array_length(projection->'observation_inputs') <> 1
     OR observation_input->>'stance' <> 'supports'
     OR identity->>'predicate' <> 'occupation.works_as'
     OR identity->>'object_kind' <> 'entity'
     OR identity->'object_literal_sha256' <> 'null'::jsonb
     OR identity->>'polarity' <> 'affirmed'
     OR identity->>'modality' <> 'asserted'
     OR projection#>>'{target,action}' <> 'create'
     OR projection#>'{target,aggregate_id}' <> 'null'::jsonb
     OR projection#>'{target,expected_revision_number}' <> 'null'::jsonb
     OR projection#>'{target,reason_codes}' <> '[]'::jsonb
     OR projection#>>'{temporal_policy,canonical_source}'
          <> 'memory.observation_temporal'
     OR projection#>>'{temporal_policy,materialization}' <> 'link_only'
     OR projection#>'{temporal_policy,source_observation_id}' <> 'null'::jsonb
     OR projection#>>'{review,state}' <> 'manual_review_required'
     OR (projection#>>'{review,authorization_required}')::boolean IS NOT TRUE
     OR projection#>'{review,reason_codes}'
          <> '["initial_live_projection_requires_review"]'::jsonb
     OR projection->'relations' <> '[]'::jsonb
     OR payload->>'kind' <> 'claim'
     OR payload->>'claim_class' <> 'direct_claim'
     OR payload->>'canonical_text'
          <> 'The user works as a personal trainer.'
     OR payload->>'surface_policy' <> 'direct_or_relevant' THEN
    RAISE EXCEPTION 'projection packet body is outside the supported contract'
      USING ERRCODE = '23514';
  END IF;
  observation_value := (observation_input->>'observation_id')::uuid;
  semantic_value := memory.v5_projection_semantic_key_sha256(
    actor,'claim',
    (identity->>'subject_entity_id')::uuid,
    identity->>'predicate',
    identity->>'object_kind',
    (identity->>'object_entity_id')::uuid,
    NULL,
    (identity->>'polarity')::memory.observation_polarity,
    (identity->>'modality')::memory.observation_modality,
    '{}'::jsonb
  );
  projection_hash := memory.v5_digest_text(
    memory.v5_canonical_json_text(projection)
  );
  packet_hash := memory.v5_digest_text(
    memory.v5_canonical_json_text(packet-'packet_sha256')
  );
  owner_manifest := memory.v5_projection_owner_manifest_sha256(
    actor,packet_hash
  );
  IF identity->>'semantic_key_sha256' <> semantic_value
     OR packet->>'packet_sha256' <> packet_hash THEN
    RAISE EXCEPTION 'projection packet hash mismatch'
      USING ERRCODE = '23514';
  END IF;
  SELECT count(*) INTO source_count
  FROM memory.observation AS observation
  JOIN memory.observation_entity_binding AS binding
    ON binding.owner_user_id=observation.owner_user_id
   AND binding.observation_id=observation.observation_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
  JOIN memory.observation_temporal AS temporal
    ON temporal.owner_user_id=observation.owner_user_id
   AND temporal.observation_id=observation.observation_id
  WHERE observation.owner_user_id=actor
    AND observation.observation_id=observation_value
    AND observation.observation_sha256
          = observation_input->>'observation_sha256'
    AND observation.predicate=identity->>'predicate'
    AND observation.polarity::text=identity->>'polarity'
    AND observation.modality::text=identity->>'modality'
    AND observation.projection_class='direct_claim'
    AND observation.surface_policy='direct_or_relevant'
    AND binding.subject_entity_id
          =(identity->>'subject_entity_id')::uuid
    AND binding.object_entity_id=(identity->>'object_entity_id')::uuid
    AND evidence.status='active';
  IF source_count <> 1 THEN
    RAISE EXCEPTION 'projection packet source binding is incomplete or stale'
      USING ERRCODE = 'P0002';
  END IF;
  SELECT count(*) INTO claim_count
  FROM memory.claim
  WHERE owner_user_id=actor AND canonical_key='v5:'||semantic_value;
  SELECT count(*) INTO plan_count
  FROM memory.projection_plan AS stored_plan
  WHERE stored_plan.owner_user_id=actor
    AND (
      stored_plan.plan_id=p_plan_id
      OR stored_plan.packet_sha256=packet_hash
    );
  RETURN QUERY SELECT
    memory.v5_digest_text(p_packet_text),
    semantic_value,projection_hash,packet_hash,owner_manifest,
    claim_count,plan_count;
END
$function$;

ALTER FUNCTION memory.preflight_projection_source_v5(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_projection_packet_v5(uuid,text)
  OWNER TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.preflight_projection_source_v5(uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_projection_packet_v5(uuid,text)
  FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_projection_source_v5(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_projection_packet_v5(uuid,text)
  TO brains_app;

COMMIT;
