BEGIN;

DO $prerequisite$
BEGIN
  IF current_user<>'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regclass('memory.project_component_entity_binding_v5') IS NULL
     OR to_regclass('memory.projection_project_payload') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.v5_jsonb_exact_keys(jsonb,text[])') IS NULL
     OR to_regprocedure('memory.v5_canonical_json_text(jsonb)') IS NULL
     OR to_regprocedure('memory.v5_projection_semantic_key_sha256(uuid,memory.projection_lane_v5,uuid,text,text,uuid,text,memory.observation_polarity,memory.observation_modality,jsonb)') IS NULL
     OR to_regprocedure('memory.v5_projection_owner_manifest_sha256(uuid,text)') IS NULL THEN
    RAISE EXCEPTION 'V5 project projection API prerequisites are absent';
  END IF;
END
$prerequisite$;

CREATE OR REPLACE FUNCTION memory.preflight_project_projection_source_v5(
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
  object_literal jsonb,
  object_literal_sha256 text,
  subject_entity_id uuid,
  subject_entity_type text,
  subject_entity_status text,
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
  project_id uuid,
  project_key text,
  component_key text,
  binding_source text
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
    observation.object_literal,
    memory.v5_digest_text(memory.v5_canonical_json_text(
      observation.object_literal
    )),
    binding.subject_entity_id,
    subject_entity.entity_type,
    subject_entity.status::text,
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
    project.project_id,
    project.project_key,
    component.component_key,
    observation.project_scope->>'binding_source'
  FROM memory.observation AS observation
  JOIN memory.observation_entity_binding AS binding
    ON binding.owner_user_id=observation.owner_user_id
   AND binding.observation_id=observation.observation_id
  JOIN memory.entity AS subject_entity
    ON subject_entity.owner_user_id=binding.owner_user_id
   AND subject_entity.entity_id=binding.subject_entity_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
  JOIN memory.observation_temporal AS temporal
    ON temporal.owner_user_id=observation.owner_user_id
   AND temporal.observation_id=observation.observation_id
  JOIN memory.project_space AS project
    ON project.owner_user_id=observation.owner_user_id
   AND project.project_key=observation.project_scope->>'project_key'
  JOIN memory.project_component_v5 AS component
    ON component.owner_user_id=project.owner_user_id
   AND component.project_id=project.project_id
   AND component.component_key=observation.project_scope->>'component_key'
  JOIN memory.project_component_entity_binding_v5 AS trusted_entity
    ON trusted_entity.owner_user_id=component.owner_user_id
   AND trusted_entity.project_id=component.project_id
   AND trusted_entity.component_id=component.component_id
   AND trusted_entity.entity_id=binding.subject_entity_id
  WHERE observation.owner_user_id=actor
    AND observation.observation_id=p_observation_id
    AND observation.projection_class='project_knowledge'
    AND observation.surface_policy='exact_project_scope_only'
    AND memory.v5_project_scope_valid(observation.project_scope)
    AND observation.project_scope->>'binding_source'
          ='trusted_component_registry'
    AND binding.object_entity_id IS NULL
    AND subject_entity.entity_type='project'
    AND subject_entity.status='active'
    AND evidence.status='active'
    AND evidence.content IS NOT NULL
    AND memory.v5_digest_text(evidence.content)=evidence.content_sha256
    AND jsonb_typeof(observation.object_literal)='object'
    AND observation.object_literal->>'kind'='literal'
    AND observation.object_literal->>'datatype'='text'
    AND btrim(COALESCE(observation.object_literal->>'value',''))<>'';
  GET DIAGNOSTICS matched=ROW_COUNT;
  IF matched<>1 THEN
    RAISE EXCEPTION 'complete owner-scoped project projection source not found'
      USING ERRCODE='P0002';
  END IF;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_project_projection_packet_v5(
  p_plan_id uuid,
  p_packet_text text
)
RETURNS TABLE(
  packet_text_sha256 text,
  semantic_key_sha256 text,
  projection_sha256 text,
  packet_sha256 text,
  owner_manifest_sha256 text,
  existing_aggregates integer,
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
  scope jsonb;
  semantic_value text;
  projection_hash text;
  packet_hash text;
  owner_manifest text;
  aggregate_count integer;
  plan_count integer;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_plan_id IS NULL OR btrim(COALESCE(p_packet_text,''))='' THEN
    RAISE EXCEPTION 'project projection preflight identifiers are required'
      USING ERRCODE='22023';
  END IF;
  BEGIN
    packet:=p_packet_text::jsonb;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'project projection packet is not valid JSON'
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
     OR packet->>'projector'<>'memory_v1_deterministic_project_projection_v5'
     OR packet->>'projector_version'<>'project_current_state_v1'
     OR jsonb_typeof(packet->'projections')<>'array'
     OR jsonb_array_length(packet->'projections')<>1 THEN
    RAISE EXCEPTION 'project projection packet envelope is outside the contract'
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
       'kind','project_id','component_key','binding_source',
       'knowledge_kind','knowledge_key','canonical_text','document_state',
       'authority_level','surface_policy'
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
     OR projection->>'lane'<>'project_knowledge'
     OR jsonb_array_length(projection->'observation_inputs')<>1
     OR observation_input->>'stance'<>'supports'
     OR identity->>'predicate'<>'project.current_state'
     OR identity->>'object_kind'<>'literal'
     OR identity->'object_entity_id'<>'null'::jsonb
     OR NOT memory.v5_sha256_valid(identity->>'object_literal_sha256')
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
          <>'["project_policy_requires_review"]'::jsonb
     OR projection->'relations'<>'[]'::jsonb
     OR payload->>'kind'<>'project_knowledge'
     OR payload->>'knowledge_kind'<>'current_state'
     OR payload->>'document_state'<>'working'
     OR payload->>'authority_level'<>'user_reported'
     OR payload->>'surface_policy'<>'exact_project_scope_only'
     OR payload->>'binding_source'<>'trusted_component_registry'
     OR payload->>'knowledge_key' !~ '^[a-z][a-z0-9_.:-]{1,239}$' THEN
    RAISE EXCEPTION 'project projection packet body is outside the contract'
      USING ERRCODE='23514';
  END IF;
  SELECT * INTO source
  FROM memory.preflight_project_projection_source_v5(
    (observation_input->>'observation_id')::uuid
  );
  IF source.observation_sha256<>observation_input->>'observation_sha256'
     OR source.subject_entity_id<>(identity->>'subject_entity_id')::uuid
     OR source.predicate<>identity->>'predicate'
     OR source.polarity<>identity->>'polarity'
     OR source.modality<>identity->>'modality'
     OR source.object_literal_sha256<>identity->>'object_literal_sha256'
     OR source.project_id<>(payload->>'project_id')::uuid
     OR source.component_key<>payload->>'component_key'
     OR source.binding_source<>payload->>'binding_source'
     OR source.object_literal->>'value'<>payload->>'canonical_text' THEN
    RAISE EXCEPTION 'project projection packet source binding is stale'
      USING ERRCODE='23514';
  END IF;
  scope:=jsonb_build_object(
    'project_id',payload->>'project_id',
    'component_key',payload->'component_key',
    'binding_source',payload->>'binding_source',
    'knowledge_kind',payload->>'knowledge_kind',
    'knowledge_key',payload->>'knowledge_key'
  );
  semantic_value:=memory.v5_projection_semantic_key_sha256(
    actor,'project_knowledge',
    (identity->>'subject_entity_id')::uuid,
    identity->>'predicate',identity->>'object_kind',NULL,
    identity->>'object_literal_sha256',
    (identity->>'polarity')::memory.observation_polarity,
    (identity->>'modality')::memory.observation_modality,
    scope
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
    RAISE EXCEPTION 'project projection packet hash mismatch'
      USING ERRCODE='23514';
  END IF;
  SELECT count(*) INTO aggregate_count
  FROM memory.project_knowledge_head_v5 AS head
  WHERE head.owner_user_id=actor
    AND head.project_id=source.project_id
    AND head.component_key IS NOT DISTINCT FROM source.component_key
    AND head.knowledge_kind=payload->>'knowledge_kind'
    AND head.knowledge_key=payload->>'knowledge_key';
  SELECT count(*) INTO plan_count
  FROM memory.projection_plan AS stored_plan
  WHERE stored_plan.owner_user_id=actor
    AND (stored_plan.plan_id=p_plan_id OR stored_plan.packet_sha256=packet_hash);
  RETURN QUERY SELECT
    memory.v5_digest_text(p_packet_text),semantic_value,
    projection_hash,packet_hash,owner_manifest,
    aggregate_count,plan_count;
END
$function$;

CREATE OR REPLACE FUNCTION memory.stage_project_projection_plan_v5(
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
  scope_value jsonb;
  result_value jsonb;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_plan_id IS NULL
     OR NOT memory.v5_sha256_valid(p_expected_owner_manifest_sha256) THEN
    RAISE EXCEPTION 'project projection stage identifiers are invalid'
      USING ERRCODE='22023';
  END IF;
  packet:=p_packet_text::jsonb;
  projection_value:=packet->'projections'->0;
  identity_value:=projection_value->'identity';
  payload_value:=projection_value->'payload';
  observation_input_value:=projection_value->'observation_inputs'->0;
  scope_value:=jsonb_build_object(
    'project_id',payload_value->>'project_id',
    'component_key',payload_value->'component_key',
    'binding_source',payload_value->>'binding_source',
    'knowledge_kind',payload_value->>'knowledge_kind',
    'knowledge_key',payload_value->>'knowledge_key'
  );
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|project-projection-plan|'||p_plan_id::text,0
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
       )
       OR NOT EXISTS (
         SELECT 1 FROM memory.projection_project_payload AS project_payload
         WHERE project_payload.owner_user_id=actor
           AND project_payload.plan_id=p_plan_id
           AND project_payload.projection_ref='p01'
           AND project_payload.canonical_text=payload_value->>'canonical_text'
           AND project_payload.project_id=(payload_value->>'project_id')::uuid
           AND project_payload.component_key=payload_value->>'component_key'
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
      RAISE EXCEPTION 'project projection stage replay state mismatch'
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
  FROM memory.preflight_project_projection_packet_v5(p_plan_id,p_packet_text);
  IF preflight.existing_aggregates<>0 OR preflight.existing_plans<>0
     OR preflight.owner_manifest_sha256<>p_expected_owner_manifest_sha256 THEN
    RAISE EXCEPTION 'project projection stage preflight is stale or mismatched'
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
    'project_knowledge',projection_value,preflight.projection_sha256,
    (identity_value->>'subject_entity_id')::uuid,
    identity_value->>'predicate',identity_value->>'object_kind',NULL,
    identity_value->>'object_literal_sha256',
    (identity_value->>'polarity')::memory.observation_polarity,
    (identity_value->>'modality')::memory.observation_modality,
    scope_value,preflight.semantic_key_sha256,'create',NULL,
    projection_value#>'{target,reason_codes}','link_only',NULL,
    'manual_review_required',true,
    projection_value#>'{review,reason_codes}'
  );
  INSERT INTO memory.projection_project_payload(
    owner_user_id,plan_id,projection_ref,target_action,
    project_id,component_key,binding_source,target_knowledge_id,
    knowledge_kind,knowledge_key,canonical_text,document_state,
    authority_level,surface_policy
  ) VALUES (
    actor,p_plan_id,'p01','create',
    (payload_value->>'project_id')::uuid,payload_value->>'component_key',
    payload_value->>'binding_source',NULL,
    payload_value->>'knowledge_kind',payload_value->>'knowledge_key',
    payload_value->>'canonical_text',payload_value->>'document_state',
    payload_value->>'authority_level',
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

ALTER FUNCTION memory.preflight_project_projection_source_v5(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_project_projection_packet_v5(uuid,text)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.stage_project_projection_plan_v5(uuid,text,text)
  OWNER TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.preflight_project_projection_source_v5(uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_project_projection_packet_v5(uuid,text)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.stage_project_projection_plan_v5(uuid,text,text)
  FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_project_projection_source_v5(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_project_projection_packet_v5(uuid,text)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.stage_project_projection_plan_v5(uuid,text,text)
  TO brains_app;

COMMIT;
