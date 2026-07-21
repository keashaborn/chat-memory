BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regclass('memory.projection_plan') IS NULL
     OR to_regclass('memory.projection_plan_item') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.observation_entailment_allows_projection_v5(uuid,text)') IS NULL THEN
    RAISE EXCEPTION 'V5.1 relationship claim projection prerequisites are absent';
  END IF;
END
$preflight$;

ALTER TABLE memory.projection_plan
  DROP CONSTRAINT projection_plan_predicate_registry_version_check;
ALTER TABLE memory.projection_plan
  ADD CONSTRAINT projection_plan_predicate_registry_version_check
  CHECK (predicate_registry_version IN (
    'memory_predicate_registry_v5','memory_predicate_registry_v5_1'
  )) NOT VALID;
ALTER TABLE memory.projection_plan
  VALIDATE CONSTRAINT projection_plan_predicate_registry_version_check;

ALTER TABLE memory.projection_plan_item
  DROP CONSTRAINT projection_plan_item_predicate_registry_version_check;
ALTER TABLE memory.projection_plan_item
  ADD CONSTRAINT projection_plan_item_predicate_registry_version_check
  CHECK (predicate_registry_version IN (
    'memory_predicate_registry_v5','memory_predicate_registry_v5_1'
  )) NOT VALID;
ALTER TABLE memory.projection_plan_item
  VALIDATE CONSTRAINT projection_plan_item_predicate_registry_version_check;

CREATE OR REPLACE FUNCTION memory.render_relationship_claim_text_v5_1(
  p_subject_entity_type text,
  p_subject_canonical_name text,
  p_predicate text,
  p_object_entity_type text,
  p_object_canonical_name text
)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  subject_label text;
  object_label text;
  result_value text;
BEGIN
  IF p_subject_entity_type IS NULL
     OR p_object_entity_type IS NULL
     OR btrim(COALESCE(p_subject_canonical_name,''))=''
     OR btrim(COALESCE(p_object_canonical_name,''))=''
     OR length(p_subject_canonical_name)>500
     OR length(p_object_canonical_name)>500
     OR p_subject_canonical_name ~ '[[:cntrl:]]'
     OR p_object_canonical_name ~ '[[:cntrl:]]' THEN
    RAISE EXCEPTION 'relationship entities are unsafe for deterministic rendering'
      USING ERRCODE='23514';
  END IF;
  subject_label:=CASE WHEN p_subject_entity_type='self'
    THEN 'The user' ELSE p_subject_canonical_name END;
  object_label:=CASE WHEN p_object_entity_type='self'
    THEN 'the user' ELSE p_object_canonical_name END;

  result_value:=CASE p_predicate
    WHEN 'relationship.acquaintance_of' THEN
      format('%s and %s are acquaintances.',subject_label,object_label)
    WHEN 'relationship.aunt_or_uncle_of' THEN
      format('%s is an aunt or uncle of %s.',subject_label,object_label)
    WHEN 'relationship.business_partner_of' THEN
      format('%s and %s are business partners.',subject_label,object_label)
    WHEN 'relationship.caregiver_for' THEN
      format('%s is a caregiver for %s.',subject_label,object_label)
    WHEN 'relationship.coach_of' THEN
      format('%s is a coach of %s.',subject_label,object_label)
    WHEN 'relationship.collaborator_with' THEN
      format('%s and %s collaborate.',subject_label,object_label)
    WHEN 'relationship.cousin_of' THEN
      format('%s and %s are cousins.',subject_label,object_label)
    WHEN 'relationship.coworker_of' THEN
      format('%s and %s are coworkers.',subject_label,object_label)
    WHEN 'relationship.friend_of' THEN
      format('%s and %s are friends.',subject_label,object_label)
    WHEN 'relationship.grandparent_of' THEN
      format('%s is a grandparent of %s.',subject_label,object_label)
    WHEN 'relationship.guardian_of' THEN
      format('%s is a guardian of %s.',subject_label,object_label)
    WHEN 'relationship.has_pet' THEN
      CASE WHEN p_object_entity_type='animal' THEN
        format('%s has a pet named %s.',subject_label,p_object_canonical_name)
      ELSE NULL END
    WHEN 'relationship.healthcare_provider_for' THEN
      format('%s is a healthcare provider for %s.',subject_label,object_label)
    WHEN 'relationship.in_law_of' THEN
      format('%s and %s are related by marriage.',subject_label,object_label)
    WHEN 'relationship.lives_with' THEN
      format('%s and %s live together.',subject_label,object_label)
    WHEN 'relationship.manager_of' THEN
      format('%s is a manager of %s.',subject_label,object_label)
    WHEN 'relationship.mentor_of' THEN
      format('%s is a mentor of %s.',subject_label,object_label)
    WHEN 'relationship.neighbor_of' THEN
      format('%s and %s are neighbors.',subject_label,object_label)
    WHEN 'relationship.parent_of' THEN
      CASE WHEN p_object_entity_type='self' THEN
        format('%s is the user''s parent.',p_subject_canonical_name)
      ELSE format('%s is a parent of %s.',subject_label,object_label) END
    WHEN 'relationship.plan_helper_for' THEN
      format('%s helps %s with planning.',subject_label,object_label)
    WHEN 'relationship.relative_of' THEN
      format('%s and %s are relatives.',subject_label,object_label)
    WHEN 'relationship.romantic_partner_of' THEN
      format('%s and %s are romantic partners.',subject_label,object_label)
    WHEN 'relationship.roommate_of' THEN
      format('%s and %s are roommates.',subject_label,object_label)
    WHEN 'relationship.sibling_of' THEN
      format('%s and %s are siblings.',subject_label,object_label)
    WHEN 'relationship.spouse_of' THEN
      format('%s and %s are spouses.',subject_label,object_label)
    WHEN 'relationship.teacher_of' THEN
      format('%s is a teacher of %s.',subject_label,object_label)
    WHEN 'relationship.teammate_of' THEN
      format('%s and %s are teammates.',subject_label,object_label)
    WHEN 'relationship.training_partner_of' THEN
      format('%s and %s are training partners.',subject_label,object_label)
    WHEN 'social.avoids' THEN
      format('%s avoids %s.',subject_label,object_label)
    WHEN 'social.competes_with' THEN
      format('%s competes with %s.',subject_label,object_label)
    WHEN 'social.depends_on' THEN
      format('%s depends on %s.',subject_label,object_label)
    WHEN 'social.distrusts' THEN
      format('%s distrusts %s.',subject_label,object_label)
    WHEN 'social.estranged_from' THEN
      format('%s is estranged from %s.',subject_label,object_label)
    WHEN 'social.experiences_tension_with' THEN
      format('%s experiences tension with %s.',subject_label,object_label)
    WHEN 'social.feels_close_to' THEN
      format('%s feels close to %s.',subject_label,object_label)
    WHEN 'social.feels_unsafe_with' THEN
      format('%s feels unsafe with %s.',subject_label,object_label)
    WHEN 'social.in_conflict_with' THEN
      format('%s is in conflict with %s.',subject_label,object_label)
    WHEN 'social.no_contact_with' THEN
      format('%s has no contact with %s.',subject_label,object_label)
    WHEN 'social.perceives_as_adversary' THEN
      format('%s perceives %s as an adversary.',subject_label,object_label)
    WHEN 'social.supports' THEN
      format('%s supports %s.',subject_label,object_label)
    WHEN 'social.trusts' THEN
      format('%s trusts %s.',subject_label,object_label)
    ELSE NULL
  END;
  IF result_value IS NULL
     OR btrim(result_value)<>result_value
     OR result_value=''
     OR length(result_value)>2000
     OR result_value ~ '[[:cntrl:]]' THEN
    RAISE EXCEPTION 'predicate has no safe V5.1 relationship renderer'
      USING ERRCODE='23514';
  END IF;
  RETURN result_value;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_relationship_claim_source_v5_1(
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
  sensitivity text,
  extraction_confidence text,
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
    observation.observation_id,observation.observation_sha256,
    observation.evidence_id,observation.predicate,
    observation.predicate_registry_version,observation.polarity::text,
    observation.modality::text,observation.projection_class::text,
    observation.surface_policy::text,observation.sensitivity::text,
    observation.extraction_confidence::text,
    binding.subject_entity_id,subject_entity.entity_type,
    subject_entity.status::text,subject_entity.canonical_name,
    binding.object_entity_id,object_entity.entity_type,
    object_entity.status::text,object_entity.canonical_name,
    evidence.content_sha256,evidence.status::text,evidence.observed_at,
    temporal.semantic::text,temporal.shape::text,temporal.basis::text,
    temporal.source_form::text,temporal.certainty::text,
    temporal.precision::text,temporal.normalized_sha256,
    observation.source_spans,
    memory.render_relationship_claim_text_v5_1(
      subject_entity.entity_type,subject_entity.canonical_name,
      observation.predicate,object_entity.entity_type,
      object_entity.canonical_name
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
    AND observation.predicate_registry_version='memory_predicate_registry_v5_1'
    AND (observation.predicate LIKE 'relationship.%'
      OR observation.predicate LIKE 'social.%')
    AND contract.contract ? 'relationship_policy'
    AND contract.object_kind='entity'
    AND contract.lifecycle='active'
    AND contract.extraction_allowed
    AND observation.polarity='affirmed'
    AND observation.modality IN ('asserted','reported_observation')
    AND (contract.contract->'modalities') ? observation.modality::text
    AND observation.projection_class IN ('direct_claim','supportive_context')
    AND (contract.contract->'projection_classes')
          ? observation.projection_class::text
    AND (contract.contract->'surface_policies')
          ? observation.surface_policy::text
    AND memory.v5_project_scope_valid(observation.project_scope)
    AND observation.project_scope->>'state'='not_applicable'
    AND observation.object_mention_id IS NOT NULL
    AND observation.object_literal IS NULL
    AND binding.object_entity_id IS NOT NULL
    AND subject_entity.status='active'
    AND object_entity.status='active'
    AND (contract.contract#>'{relationship_policy,subject_entity_types}')
          ? subject_entity.entity_type
    AND (contract.contract#>'{relationship_policy,object_entity_types}')
          ? object_entity.entity_type
    AND evidence.status='active'
    AND evidence.content IS NOT NULL
    AND memory.v5_digest_text(evidence.content)=evidence.content_sha256
    AND memory.observation_entailment_allows_projection_v5(
      observation.observation_id,observation.observation_sha256
    );
  GET DIAGNOSTICS matched=ROW_COUNT;
  IF matched<>1 THEN
    RAISE EXCEPTION 'complete accepted owner-scoped V5.1 relationship source not found'
      USING ERRCODE='P0002';
  END IF;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_relationship_claim_packet_v5_1(
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
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_plan_id IS NULL OR btrim(COALESCE(p_packet_text,''))='' THEN
    RAISE EXCEPTION 'relationship claim projection identifiers are required'
      USING ERRCODE='22023';
  END IF;
  BEGIN
    packet:=p_packet_text::jsonb;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'relationship claim projection packet is not valid JSON'
      USING ERRCODE='22023';
  END;
  IF NOT memory.v5_jsonb_exact_keys(packet,ARRAY[
       'contract_version','predicate_registry_version',
       'projection_policy_version','projector','projector_version',
       'projections','packet_sha256'
     ])
     OR packet->>'contract_version'<>'memory_v1_projection_plan_v5'
     OR packet->>'predicate_registry_version'<>'memory_predicate_registry_v5_1'
     OR packet->>'projection_policy_version'<>'memory_projection_policy_v5'
     OR packet->>'projector'<>'memory_v1_deterministic_relationship_claim_projection_v5_1'
     OR packet->>'projector_version'<>'relationship_template_v1'
     OR jsonb_typeof(packet->'projections')<>'array'
     OR jsonb_array_length(packet->'projections')<>1 THEN
    RAISE EXCEPTION 'relationship claim envelope is outside the V5.1 contract'
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
     OR (identity->>'predicate' NOT LIKE 'relationship.%'
       AND identity->>'predicate' NOT LIKE 'social.%')
     OR identity->>'object_kind'<>'entity'
     OR identity->'object_entity_id'='null'::jsonb
     OR identity->'object_literal_sha256'<>'null'::jsonb
     OR identity->>'polarity'<>'affirmed'
     OR identity->>'modality' NOT IN ('asserted','reported_observation')
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
          <>'["initial_relationship_claim_projection_requires_review"]'::jsonb
     OR projection->'relations'<>'[]'::jsonb
     OR payload->>'kind'<>'claim'
     OR payload->>'claim_class' NOT IN ('direct_claim','supportive_context')
     OR btrim(COALESCE(payload->>'canonical_text',''))=''
     OR length(payload->>'canonical_text')>2000 THEN
    RAISE EXCEPTION 'relationship claim body is outside the V5.1 contract'
      USING ERRCODE='23514';
  END IF;
  input_object_entity_id:=(identity->>'object_entity_id')::uuid;
  SELECT * INTO source
  FROM memory.preflight_relationship_claim_source_v5_1(
    (observation_input->>'observation_id')::uuid
  );
  IF source.observation_sha256<>observation_input->>'observation_sha256'
     OR source.predicate_registry_version<>packet->>'predicate_registry_version'
     OR source.subject_entity_id<>(identity->>'subject_entity_id')::uuid
     OR source.predicate<>identity->>'predicate'
     OR source.object_entity_id<>input_object_entity_id
     OR source.polarity<>identity->>'polarity'
     OR source.modality<>identity->>'modality'
     OR source.projection_class<>payload->>'claim_class'
     OR source.surface_policy<>payload->>'surface_policy'
     OR source.canonical_text<>payload->>'canonical_text' THEN
    RAISE EXCEPTION 'relationship claim source binding is stale or forged'
      USING ERRCODE='23514';
  END IF;
  semantic_value:=memory.v5_projection_semantic_key_sha256(
    actor,'claim',(identity->>'subject_entity_id')::uuid,
    identity->>'predicate','entity',input_object_entity_id,NULL,
    (identity->>'polarity')::memory.observation_polarity,
    (identity->>'modality')::memory.observation_modality,'{}'::jsonb
  );
  projection_hash:=memory.v5_digest_text(
    memory.v5_canonical_json_text(projection)
  );
  packet_hash:=memory.v5_digest_text(
    memory.v5_canonical_json_text(packet-'packet_sha256')
  );
  owner_manifest:=memory.v5_projection_owner_manifest_sha256(actor,packet_hash);
  IF identity->>'semantic_key_sha256'<>semantic_value
     OR packet->>'packet_sha256'<>packet_hash THEN
    RAISE EXCEPTION 'relationship claim packet hash mismatch'
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

CREATE OR REPLACE FUNCTION memory.stage_relationship_claim_plan_v5_1(
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
  result_value jsonb;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_plan_id IS NULL
     OR NOT memory.v5_sha256_valid(p_expected_owner_manifest_sha256) THEN
    RAISE EXCEPTION 'relationship claim stage identifiers are invalid'
      USING ERRCODE='22023';
  END IF;
  packet:=p_packet_text::jsonb;
  projection_value:=packet->'projections'->0;
  identity_value:=projection_value->'identity';
  payload_value:=projection_value->'payload';
  observation_input_value:=projection_value->'observation_inputs'->0;
  object_entity_value:=(identity_value->>'object_entity_id')::uuid;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|relationship-claim-plan-v5-1|'||p_plan_id::text,0
  ));
  SELECT stored.* INTO existing
  FROM memory.projection_plan AS stored
  WHERE stored.owner_user_id=actor
    AND (stored.plan_id=p_plan_id OR stored.packet_sha256=packet->>'packet_sha256');
  IF FOUND THEN
    IF existing.plan_id<>p_plan_id
       OR existing.packet_text<>p_packet_text
       OR existing.owner_manifest_sha256<>p_expected_owner_manifest_sha256
       OR existing.predicate_registry_version<>'memory_predicate_registry_v5_1'
       OR (SELECT count(*) FROM memory.projection_plan_item AS item
           WHERE item.owner_user_id=actor AND item.plan_id=p_plan_id)<>1
       OR NOT EXISTS (
         SELECT 1 FROM memory.projection_plan_item AS item
         WHERE item.owner_user_id=actor AND item.plan_id=p_plan_id
           AND item.projection_ref='p01'
           AND item.projection=projection_value
           AND item.object_entity_id=object_entity_value
           AND item.object_literal_sha256 IS NULL
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
      RAISE EXCEPTION 'relationship claim stage replay state mismatch'
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
  FROM memory.preflight_relationship_claim_packet_v5_1(
    p_plan_id,p_packet_text
  );
  IF preflight.existing_claims<>0 OR preflight.existing_plans<>0
     OR preflight.owner_manifest_sha256<>p_expected_owner_manifest_sha256 THEN
    RAISE EXCEPTION 'relationship claim stage preflight is stale or mismatched'
      USING ERRCODE='23514';
  END IF;
  INSERT INTO memory.projection_plan(
    owner_user_id,plan_id,contract_version,predicate_registry_version,
    projection_policy_version,projector,projector_version,
    packet_text,packet_text_sha256,packet_sha256,
    owner_manifest_sha256,projection_count,invoked_by_session
  ) VALUES (
    actor,p_plan_id,packet->>'contract_version',
    packet->>'predicate_registry_version',packet->>'projection_policy_version',
    packet->>'projector',packet->>'projector_version',p_packet_text,
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
    identity_value->>'predicate','entity',object_entity_value,NULL,
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

ALTER FUNCTION memory.render_relationship_claim_text_v5_1(
  text,text,text,text,text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_relationship_claim_source_v5_1(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_relationship_claim_packet_v5_1(uuid,text)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.stage_relationship_claim_plan_v5_1(uuid,text,text)
  OWNER TO memory_v5_writer;

GRANT EXECUTE ON FUNCTION memory.require_v5_writer_context()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.observation_entailment_allows_projection_v5(
  uuid,text
) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_project_scope_valid(jsonb)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_digest_text(text)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_jsonb_exact_keys(jsonb,text[])
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_canonical_json_text(jsonb)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_projection_semantic_key_sha256(
  uuid,memory.projection_lane_v5,uuid,text,text,uuid,text,
  memory.observation_polarity,memory.observation_modality,jsonb
) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_projection_owner_manifest_sha256(uuid,text)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_sha256_valid(text)
  TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.render_relationship_claim_text_v5_1(
  text,text,text,text,text
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_relationship_claim_source_v5_1(uuid)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.preflight_relationship_claim_packet_v5_1(uuid,text)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.stage_relationship_claim_plan_v5_1(uuid,text,text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.preflight_relationship_claim_source_v5_1(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_relationship_claim_packet_v5_1(uuid,text)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.stage_relationship_claim_plan_v5_1(uuid,text,text)
  TO brains_app;

COMMIT;
