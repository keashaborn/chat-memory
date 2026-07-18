BEGIN;

DO $prerequisite$
BEGIN
  IF current_user<>'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regclass('memory.observation') IS NULL
     OR to_regclass('memory.observation_entity_binding') IS NULL
     OR to_regclass('memory.observation_temporal') IS NULL
     OR to_regclass('memory.projection_plan') IS NULL
     OR to_regclass('memory.projection_claim_payload') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.v5_jsonb_exact_keys(jsonb,text[])') IS NULL
     OR to_regprocedure('memory.v5_canonical_json_text(jsonb)') IS NULL
     OR to_regprocedure('memory.v5_projection_semantic_key_sha256(uuid,memory.projection_lane_v5,uuid,text,text,uuid,text,memory.observation_polarity,memory.observation_modality,jsonb)') IS NULL
     OR to_regprocedure('memory.v5_projection_owner_manifest_sha256(uuid,text)') IS NULL THEN
    RAISE EXCEPTION 'V5.1 claim projection API prerequisites are absent';
  END IF;
END
$prerequisite$;

CREATE OR REPLACE FUNCTION memory.v5_claim_literal_value_v5_1(
  p_predicate text,
  p_literal jsonb
)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  value_text text;
  expected_datatype text;
BEGIN
  expected_datatype:=CASE p_predicate
    WHEN 'identity.name' THEN 'text'
    WHEN 'pet.breed' THEN 'text'
    WHEN 'pet.sex' THEN 'enum'
    ELSE NULL
  END;
  IF expected_datatype IS NULL
     OR NOT memory.v5_literal_object_valid(p_literal)
     OR p_literal->>'kind'<>'literal'
     OR p_literal->>'datatype'<>expected_datatype
     OR jsonb_typeof(p_literal->'value')<>'string'
     OR p_literal->'unit'<>'null'::jsonb
     OR (p_literal->>'approximate')::boolean IS TRUE THEN
    RAISE EXCEPTION 'literal is outside the supported V5.1 claim contract'
      USING ERRCODE='23514';
  END IF;
  value_text:=p_literal->>'value';
  IF btrim(value_text)<>value_text
     OR value_text=''
     OR length(value_text)>500
     OR value_text ~ '[[:cntrl:]]' THEN
    RAISE EXCEPTION 'literal text is not safe for deterministic rendering'
      USING ERRCODE='23514';
  END IF;
  RETURN value_text;
END
$function$;

CREATE OR REPLACE FUNCTION memory.render_claim_projection_text_v5_1(
  p_subject_entity_type text,
  p_subject_canonical_name text,
  p_predicate text,
  p_object_kind text,
  p_object_entity_type text,
  p_object_canonical_name text,
  p_object_literal jsonb
)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  literal_value text;
  result_value text;
BEGIN
  IF p_subject_entity_type IS NULL
     OR btrim(COALESCE(p_subject_canonical_name,''))=''
     OR length(p_subject_canonical_name)>500
     OR p_subject_canonical_name ~ '[[:cntrl:]]' THEN
    RAISE EXCEPTION 'subject entity is unsafe for deterministic rendering'
      USING ERRCODE='23514';
  END IF;
  CASE p_predicate
    WHEN 'relationship.has_pet' THEN
      IF p_object_kind<>'entity'
         OR p_subject_entity_type NOT IN ('self','person')
         OR p_object_entity_type<>'animal'
         OR p_object_literal IS NOT NULL
         OR btrim(COALESCE(p_object_canonical_name,''))=''
         OR length(p_object_canonical_name)>500
         OR p_object_canonical_name ~ '[[:cntrl:]]' THEN
        RAISE EXCEPTION 'pet relationship is outside the rendering contract'
          USING ERRCODE='23514';
      END IF;
      IF p_subject_entity_type='self' THEN
        result_value:=format('The user has a pet named %s.',p_object_canonical_name);
      ELSE
        result_value:=format('%s has a pet named %s.',
          p_subject_canonical_name,p_object_canonical_name);
      END IF;
    WHEN 'identity.name' THEN
      IF p_object_kind<>'literal'
         OR p_object_entity_type IS NOT NULL
         OR p_object_canonical_name IS NOT NULL THEN
        RAISE EXCEPTION 'name observation is outside the rendering contract'
          USING ERRCODE='23514';
      END IF;
      literal_value:=memory.v5_claim_literal_value_v5_1(
        p_predicate,p_object_literal
      );
      result_value:=CASE p_subject_entity_type
        WHEN 'self' THEN format('The user''s name is %s.',literal_value)
        WHEN 'person' THEN format('This person''s name is %s.',literal_value)
        WHEN 'animal' THEN format('This animal''s name is %s.',literal_value)
        WHEN 'organization' THEN format('This organization''s name is %s.',literal_value)
        WHEN 'place' THEN format('This place''s name is %s.',literal_value)
        WHEN 'project' THEN format('This project''s name is %s.',literal_value)
        WHEN 'object' THEN format('This object''s name is %s.',literal_value)
        WHEN 'concept' THEN format('This concept''s name is %s.',literal_value)
        ELSE NULL
      END;
      IF result_value IS NULL THEN
        RAISE EXCEPTION 'name subject type is outside the rendering contract'
          USING ERRCODE='23514';
      END IF;
    WHEN 'pet.breed' THEN
      IF p_object_kind<>'literal'
         OR p_subject_entity_type<>'animal'
         OR p_object_entity_type IS NOT NULL
         OR p_object_canonical_name IS NOT NULL THEN
        RAISE EXCEPTION 'pet breed is outside the rendering contract'
          USING ERRCODE='23514';
      END IF;
      literal_value:=memory.v5_claim_literal_value_v5_1(
        p_predicate,p_object_literal
      );
      result_value:=format('%s has recorded breed %s.',
        p_subject_canonical_name,literal_value);
    WHEN 'pet.sex' THEN
      IF p_object_kind<>'literal'
         OR p_subject_entity_type<>'animal'
         OR p_object_entity_type IS NOT NULL
         OR p_object_canonical_name IS NOT NULL THEN
        RAISE EXCEPTION 'pet sex is outside the rendering contract'
          USING ERRCODE='23514';
      END IF;
      literal_value:=memory.v5_claim_literal_value_v5_1(
        p_predicate,p_object_literal
      );
      result_value:=format('%s has recorded sex %s.',
        p_subject_canonical_name,literal_value);
    ELSE
      RAISE EXCEPTION 'predicate has no deterministic V5.1 claim renderer'
        USING ERRCODE='23514';
  END CASE;
  IF btrim(result_value)<>result_value
     OR result_value=''
     OR length(result_value)>2000
     OR result_value ~ '[[:cntrl:]]' THEN
    RAISE EXCEPTION 'rendered claim text is invalid'
      USING ERRCODE='23514';
  END IF;
  RETURN result_value;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_claim_projection_source_v5_1(
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
  object_kind text,
  object_literal jsonb,
  object_literal_sha256 text,
  subject_entity_id uuid,
  subject_entity_type text,
  subject_entity_status text,
  subject_canonical_name text,
  object_entity_id uuid,
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
  temporal_normalized_sha256 text,
  source_spans jsonb,
  canonical_text text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  matched integer;
BEGIN
  actor:=memory.require_v5_writer_context();
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
    contract.object_kind,
    observation.object_literal,
    CASE WHEN observation.object_literal IS NULL THEN NULL ELSE
      memory.v5_digest_text(memory.v5_canonical_json_text(
        observation.object_literal
      ))
    END,
    binding.subject_entity_id,
    subject_entity.entity_type,
    subject_entity.status::text,
    subject_entity.canonical_name,
    binding.object_entity_id,
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
    temporal.normalized_sha256,
    observation.source_spans,
    memory.render_claim_projection_text_v5_1(
      subject_entity.entity_type,subject_entity.canonical_name,
      observation.predicate,contract.object_kind,
      object_entity.entity_type,object_entity.canonical_name,
      observation.object_literal
    )
  FROM memory.observation AS observation
  JOIN memory.predicate_contract AS contract
    ON contract.predicate=observation.predicate
   AND contract.registry_version=observation.predicate_registry_version
  JOIN memory.observation_entity_binding AS binding
    ON binding.owner_user_id=observation.owner_user_id
   AND binding.observation_id=observation.observation_id
  JOIN memory.entity AS subject_entity
    ON subject_entity.owner_user_id=binding.owner_user_id
   AND subject_entity.entity_id=binding.subject_entity_id
  LEFT JOIN memory.entity AS object_entity
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
    AND observation.predicate_registry_version='memory_predicate_registry_v5'
    AND observation.predicate IN (
      'identity.name','pet.breed','pet.sex','relationship.has_pet'
    )
    AND observation.polarity='affirmed'
    AND observation.modality='asserted'
    AND observation.projection_class IN ('direct_claim','supportive_context')
    AND memory.v5_project_scope_valid(observation.project_scope)
    AND observation.project_scope->>'state'='not_applicable'
    AND (
      (observation.predicate IN ('identity.name','pet.breed','pet.sex')
       AND observation.projection_class='direct_claim'
       AND observation.surface_policy='direct_or_relevant')
      OR
      (observation.predicate='relationship.has_pet'
       AND (
         (observation.projection_class='direct_claim'
          AND observation.surface_policy='direct_or_relevant')
         OR
         (observation.projection_class='supportive_context'
          AND observation.surface_policy='mention_when_directly_relevant')
       ))
    )
    AND contract.lifecycle='active'
    AND contract.extraction_allowed
    AND subject_entity.status='active'
    AND evidence.status='active'
    AND evidence.content IS NOT NULL
    AND memory.v5_digest_text(evidence.content)=evidence.content_sha256
    AND (
      (contract.object_kind='entity'
       AND observation.object_mention_id IS NOT NULL
       AND observation.object_literal IS NULL
       AND binding.object_entity_id IS NOT NULL
       AND object_entity.status='active')
      OR
      (contract.object_kind='literal'
       AND observation.object_mention_id IS NULL
       AND observation.object_literal IS NOT NULL
       AND binding.object_entity_id IS NULL
       AND object_entity.entity_id IS NULL
       AND memory.v5_literal_object_valid(observation.object_literal))
    );
  GET DIAGNOSTICS matched=ROW_COUNT;
  IF matched<>1 THEN
    RAISE EXCEPTION 'complete owner-scoped V5.1 claim source not found'
      USING ERRCODE='P0002';
  END IF;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_claim_projection_packet_v5_1(
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
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  packet jsonb;
  projection jsonb;
  identity jsonb;
  payload jsonb;
  observation_input jsonb;
  source record;
  semantic_value text;
  projection_hash text;
  packet_hash text;
  owner_manifest text;
  claim_count integer;
  plan_count integer;
  input_object_entity_id uuid;
  input_literal_sha text;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_plan_id IS NULL OR btrim(COALESCE(p_packet_text,''))='' THEN
    RAISE EXCEPTION 'claim projection identifiers are required'
      USING ERRCODE='22023';
  END IF;
  BEGIN
    packet:=p_packet_text::jsonb;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'claim projection packet is not valid JSON'
      USING ERRCODE='22023';
  END;
  IF NOT memory.v5_jsonb_exact_keys(packet,ARRAY[
       'contract_version','predicate_registry_version',
       'projection_policy_version','projector','projector_version',
       'projections','packet_sha256'
     ])
     OR packet->>'contract_version'<>'memory_v1_projection_plan_v5'
     OR packet->>'predicate_registry_version'<>'memory_predicate_registry_v5'
     OR packet->>'projection_policy_version'<>'memory_projection_policy_v5'
     OR packet->>'projector'<>'memory_v1_deterministic_claim_projection_v5_1'
     OR packet->>'projector_version'<>'claim_template_v1'
     OR jsonb_typeof(packet->'projections')<>'array'
     OR jsonb_array_length(packet->'projections')<>1 THEN
    RAISE EXCEPTION 'claim projection envelope is outside the V5.1 contract'
      USING ERRCODE='23514';
  END IF;
  projection:=packet->'projections'->0;
  identity:=projection->'identity';
  payload:=projection->'payload';
  observation_input:=projection->'observation_inputs'->0;
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
     OR projection->>'projection_ref'<>'p01'
     OR projection->>'lane'<>'claim'
     OR jsonb_typeof(projection->'observation_inputs')<>'array'
     OR jsonb_array_length(projection->'observation_inputs')<>1
     OR observation_input->>'stance'<>'supports'
     OR identity->>'predicate' NOT IN (
       'identity.name','pet.breed','pet.sex','relationship.has_pet'
     )
     OR identity->>'object_kind' NOT IN ('entity','literal')
     OR identity->>'polarity'<>'affirmed'
     OR identity->>'modality'<>'asserted'
     OR projection#>>'{target,action}'<>'create'
     OR projection#>'{target,aggregate_id}'<>'null'::jsonb
     OR projection#>'{target,expected_revision_number}'<>'null'::jsonb
     OR projection#>'{target,reason_codes}'<>'[]'::jsonb
     OR projection#>>'{temporal_policy,canonical_source}'
          <>'memory.observation_temporal'
     OR projection#>>'{temporal_policy,materialization}'<>'link_only'
     OR projection#>'{temporal_policy,source_observation_id}'<>'null'::jsonb
     OR projection#>>'{review,state}'<>'manual_review_required'
     OR (projection#>>'{review,authorization_required}')::boolean IS NOT TRUE
     OR projection#>'{review,reason_codes}'
          <>'["initial_claim_projection_requires_review"]'::jsonb
     OR projection->'relations'<>'[]'::jsonb
     OR payload->>'kind'<>'claim'
     OR payload->>'claim_class' NOT IN ('direct_claim','supportive_context')
     OR btrim(COALESCE(payload->>'canonical_text',''))=''
     OR length(payload->>'canonical_text')>2000 THEN
    RAISE EXCEPTION 'claim projection body is outside the V5.1 contract'
      USING ERRCODE='23514';
  END IF;
  IF identity->>'object_kind'='entity' THEN
    IF identity->'object_entity_id'='null'::jsonb
       OR identity->'object_literal_sha256'<>'null'::jsonb THEN
      RAISE EXCEPTION 'entity object identity is malformed'
        USING ERRCODE='23514';
    END IF;
    input_object_entity_id:=(identity->>'object_entity_id')::uuid;
    input_literal_sha:=NULL;
  ELSE
    IF identity->'object_entity_id'<>'null'::jsonb
       OR NOT memory.v5_sha256_valid(identity->>'object_literal_sha256') THEN
      RAISE EXCEPTION 'literal object identity is malformed'
        USING ERRCODE='23514';
    END IF;
    input_object_entity_id:=NULL;
    input_literal_sha:=identity->>'object_literal_sha256';
  END IF;
  SELECT * INTO source
  FROM memory.preflight_claim_projection_source_v5_1(
    (observation_input->>'observation_id')::uuid
  );
  IF source.observation_sha256<>observation_input->>'observation_sha256'
     OR source.subject_entity_id<>(identity->>'subject_entity_id')::uuid
     OR source.predicate<>identity->>'predicate'
     OR source.object_kind<>identity->>'object_kind'
     OR source.object_entity_id IS DISTINCT FROM input_object_entity_id
     OR source.object_literal_sha256 IS DISTINCT FROM input_literal_sha
     OR source.polarity<>identity->>'polarity'
     OR source.modality<>identity->>'modality'
     OR source.projection_class<>payload->>'claim_class'
     OR source.surface_policy<>payload->>'surface_policy'
     OR source.canonical_text<>payload->>'canonical_text' THEN
    RAISE EXCEPTION 'claim projection source binding is stale or forged'
      USING ERRCODE='23514';
  END IF;
  semantic_value:=memory.v5_projection_semantic_key_sha256(
    actor,'claim',(identity->>'subject_entity_id')::uuid,
    identity->>'predicate',identity->>'object_kind',
    input_object_entity_id,input_literal_sha,
    (identity->>'polarity')::memory.observation_polarity,
    (identity->>'modality')::memory.observation_modality,'{}'::jsonb
  );
  projection_hash:=memory.v5_digest_text(
    memory.v5_canonical_json_text(projection)
  );
  packet_hash:=memory.v5_digest_text(
    memory.v5_canonical_json_text(packet-'packet_sha256')
  );
  owner_manifest:=memory.v5_projection_owner_manifest_sha256(
    actor,packet_hash
  );
  IF identity->>'semantic_key_sha256'<>semantic_value
     OR packet->>'packet_sha256'<>packet_hash THEN
    RAISE EXCEPTION 'claim projection packet hash mismatch'
      USING ERRCODE='23514';
  END IF;
  SELECT count(*) INTO claim_count
  FROM memory.claim AS claim
  WHERE claim.owner_user_id=actor
    AND claim.canonical_key='v5:'||semantic_value;
  SELECT count(*) INTO plan_count
  FROM memory.projection_plan AS stored_plan
  WHERE stored_plan.owner_user_id=actor
    AND (stored_plan.plan_id=p_plan_id OR stored_plan.packet_sha256=packet_hash);
  RETURN QUERY SELECT
    memory.v5_digest_text(p_packet_text),semantic_value,
    projection_hash,packet_hash,owner_manifest,claim_count,plan_count;
END
$function$;

CREATE OR REPLACE FUNCTION memory.stage_claim_projection_plan_v5_1(
  p_plan_id uuid,
  p_packet_text text,
  p_expected_owner_manifest_sha256 text
)
RETURNS TABLE(
  plan_id uuid,
  outcome text,
  rows_written integer,
  result jsonb
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  packet jsonb;
  projection_value jsonb;
  identity_value jsonb;
  payload_value jsonb;
  observation_input_value jsonb;
  preflight record;
  existing memory.projection_plan%ROWTYPE;
  object_entity_value uuid;
  object_literal_sha_value text;
  result_value jsonb;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_plan_id IS NULL
     OR NOT memory.v5_sha256_valid(p_expected_owner_manifest_sha256) THEN
    RAISE EXCEPTION 'claim projection stage identifiers are invalid'
      USING ERRCODE='22023';
  END IF;
  packet:=p_packet_text::jsonb;
  projection_value:=packet->'projections'->0;
  identity_value:=projection_value->'identity';
  payload_value:=projection_value->'payload';
  observation_input_value:=projection_value->'observation_inputs'->0;
  IF identity_value->>'object_kind'='entity' THEN
    object_entity_value:=(identity_value->>'object_entity_id')::uuid;
    object_literal_sha_value:=NULL;
  ELSE
    object_entity_value:=NULL;
    object_literal_sha_value:=identity_value->>'object_literal_sha256';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|claim-projection-plan-v5-1|'||p_plan_id::text,0
  ));
  SELECT stored.* INTO existing
  FROM memory.projection_plan AS stored
  WHERE stored.owner_user_id=actor
    AND (stored.plan_id=p_plan_id OR stored.packet_sha256=packet->>'packet_sha256');
  IF FOUND THEN
    IF existing.plan_id<>p_plan_id
       OR existing.packet_text<>p_packet_text
       OR existing.owner_manifest_sha256<>p_expected_owner_manifest_sha256
       OR (SELECT count(*) FROM memory.projection_plan_item AS item
           WHERE item.owner_user_id=actor AND item.plan_id=p_plan_id)<>1
       OR NOT EXISTS (
         SELECT 1 FROM memory.projection_plan_item AS item
         WHERE item.owner_user_id=actor AND item.plan_id=p_plan_id
           AND item.projection_ref='p01'
           AND item.projection=projection_value
           AND item.object_entity_id IS NOT DISTINCT FROM object_entity_value
           AND item.object_literal_sha256 IS NOT DISTINCT FROM object_literal_sha_value
       )
       OR NOT EXISTS (
         SELECT 1 FROM memory.projection_claim_payload AS claim_payload
         WHERE claim_payload.owner_user_id=actor
           AND claim_payload.plan_id=p_plan_id
           AND claim_payload.projection_ref='p01'
           AND claim_payload.canonical_text=payload_value->>'canonical_text'
           AND claim_payload.claim_class=payload_value->>'claim_class'
           AND claim_payload.surface_policy::text=payload_value->>'surface_policy'
       )
       OR NOT EXISTS (
         SELECT 1 FROM memory.projection_plan_observation AS link
         WHERE link.owner_user_id=actor AND link.plan_id=p_plan_id
           AND link.projection_ref='p01'
           AND link.observation_id
                =(observation_input_value->>'observation_id')::uuid
           AND link.observation_sha256
                =observation_input_value->>'observation_sha256'
       ) THEN
      RAISE EXCEPTION 'claim projection stage replay state mismatch'
        USING ERRCODE='23514';
    END IF;
    result_value:=jsonb_build_object(
      'plan_id',p_plan_id,'projection_ref','p01',
      'packet_sha256',existing.packet_sha256
    );
    RETURN QUERY SELECT p_plan_id,'replayed',0,result_value;
    RETURN;
  END IF;
  SELECT * INTO preflight
  FROM memory.preflight_claim_projection_packet_v5_1(p_plan_id,p_packet_text);
  IF preflight.existing_claims<>0 OR preflight.existing_plans<>0
     OR preflight.owner_manifest_sha256<>p_expected_owner_manifest_sha256 THEN
    RAISE EXCEPTION 'claim projection stage preflight is stale or mismatched'
      USING ERRCODE='23514';
  END IF;
  INSERT INTO memory.projection_plan(
    owner_user_id,plan_id,contract_version,predicate_registry_version,
    projection_policy_version,projector,projector_version,
    packet_text,packet_text_sha256,packet_sha256,
    owner_manifest_sha256,projection_count,invoked_by_session
  ) VALUES (
    actor,p_plan_id,packet->>'contract_version',
    packet->>'predicate_registry_version',
    packet->>'projection_policy_version',packet->>'projector',
    packet->>'projector_version',p_packet_text,
    preflight.packet_text_sha256,preflight.packet_sha256,
    preflight.owner_manifest_sha256,1,session_user
  );
  INSERT INTO memory.projection_plan_item(
    owner_user_id,plan_id,projection_ref,predicate_registry_version,
    lane,projection,projection_sha256,subject_entity_id,predicate,
    object_kind,object_entity_id,object_literal_sha256,polarity,
    modality,lane_scope,semantic_key_sha256,target_action,
    expected_revision_number,target_reason_codes,temporal_materialization,
    temporal_source_observation_id,review_state,authorization_required,
    review_reason_codes
  ) VALUES (
    actor,p_plan_id,'p01',packet->>'predicate_registry_version',
    'claim',projection_value,preflight.projection_sha256,
    (identity_value->>'subject_entity_id')::uuid,
    identity_value->>'predicate',identity_value->>'object_kind',
    object_entity_value,object_literal_sha_value,
    (identity_value->>'polarity')::memory.observation_polarity,
    (identity_value->>'modality')::memory.observation_modality,
    '{}'::jsonb,preflight.semantic_key_sha256,'create',NULL,
    projection_value#>'{target,reason_codes}','link_only',NULL,
    'manual_review_required',true,
    projection_value#>'{review,reason_codes}'
  );
  INSERT INTO memory.projection_claim_payload(
    owner_user_id,plan_id,projection_ref,target_action,target_claim_id,
    claim_class,canonical_text,surface_policy
  ) VALUES (
    actor,p_plan_id,'p01','create',NULL,
    payload_value->>'claim_class',payload_value->>'canonical_text',
    (payload_value->>'surface_policy')::memory.observation_surface_policy
  );
  INSERT INTO memory.projection_plan_observation(
    owner_user_id,plan_id,projection_ref,
    observation_id,observation_sha256,stance
  ) VALUES (
    actor,p_plan_id,'p01',
    (observation_input_value->>'observation_id')::uuid,
    observation_input_value->>'observation_sha256',
    (observation_input_value->>'stance')
      ::memory.projection_observation_stance_v5
  );
  SET CONSTRAINTS ALL IMMEDIATE;
  result_value:=jsonb_build_object(
    'plan_id',p_plan_id,'projection_ref','p01',
    'packet_sha256',preflight.packet_sha256
  );
  RETURN QUERY SELECT p_plan_id,'applied',4,result_value;
END
$function$;

ALTER FUNCTION memory.v5_claim_literal_value_v5_1(text,jsonb)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.render_claim_projection_text_v5_1(
  text,text,text,text,text,text,jsonb
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_claim_projection_source_v5_1(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_claim_projection_packet_v5_1(uuid,text)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.stage_claim_projection_plan_v5_1(uuid,text,text)
  OWNER TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.v5_claim_literal_value_v5_1(text,jsonb)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.render_claim_projection_text_v5_1(
  text,text,text,text,text,text,jsonb
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_claim_projection_source_v5_1(uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_claim_projection_packet_v5_1(uuid,text)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.stage_claim_projection_plan_v5_1(uuid,text,text)
  FROM PUBLIC,brains_app;

GRANT EXECUTE ON FUNCTION memory.preflight_claim_projection_source_v5_1(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_claim_projection_packet_v5_1(uuid,text)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.stage_claim_projection_plan_v5_1(uuid,text,text)
  TO brains_app;

COMMIT;
