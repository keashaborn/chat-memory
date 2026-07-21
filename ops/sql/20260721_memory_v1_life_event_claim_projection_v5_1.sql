BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
DECLARE
  source_hash text;
  packet_hash text;
BEGIN
  IF session_user <> 'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure(
       'memory.observation_entailment_allows_projection_v5(uuid,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.1 life-event claim projection prerequisites are absent';
  END IF;
  IF to_regprocedure(
       'memory.preflight_claim_projection_source_v5_1_base(uuid)'
     ) IS NULL THEN
    SELECT encode(public.digest(convert_to(prosrc,'UTF8'),'sha256'),'hex')
    INTO source_hash
    FROM pg_proc
    WHERE oid='memory.preflight_claim_projection_source_v5_1(uuid)'::regprocedure;
    SELECT encode(public.digest(convert_to(prosrc,'UTF8'),'sha256'),'hex')
    INTO packet_hash
    FROM pg_proc
    WHERE oid='memory.preflight_claim_projection_packet_v5_1(uuid,text)'::regprocedure;
    IF source_hash <> 'e908ee85c8ae0b23c1a0c45b5bbb30194d20198b3ff17ec8c9490592f3602e2f'
       OR packet_hash <> 'bc4c934fddb715a72ec2f69bba987964f10f3b7975488aec88662338a1f21131' THEN
      RAISE EXCEPTION 'installed V5.1 claim projection API changed';
    END IF;
  END IF;
END
$preflight$;

DO $rename_base$
BEGIN
  IF to_regprocedure(
       'memory.preflight_claim_projection_source_v5_1_base(uuid)'
     ) IS NULL THEN
    ALTER FUNCTION memory.preflight_claim_projection_source_v5_1(uuid)
      RENAME TO preflight_claim_projection_source_v5_1_base;
  END IF;
  IF to_regprocedure(
       'memory.preflight_claim_projection_packet_v5_1_base(uuid,text)'
     ) IS NULL THEN
    ALTER FUNCTION memory.preflight_claim_projection_packet_v5_1(uuid,text)
      RENAME TO preflight_claim_projection_packet_v5_1_base;
  END IF;
END
$rename_base$;

CREATE OR REPLACE FUNCTION memory.render_life_event_claim_text_v5_1(
  p_subject_entity_type text,
  p_subject_canonical_name text,
  p_subject_metadata jsonb,
  p_predicate text,
  p_object_literal jsonb
)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  role_value text;
  result_value text;
BEGIN
  IF p_predicate <> 'life_event.died'
     OR p_subject_entity_type NOT IN ('person','animal')
     OR btrim(COALESCE(p_subject_canonical_name,'')) = ''
     OR length(p_subject_canonical_name) > 500
     OR p_subject_canonical_name ~ '[[:cntrl:]]'
     OR NOT memory.v5_literal_object_valid(p_object_literal)
     OR p_object_literal->>'datatype' <> 'boolean'
     OR p_object_literal->'value' <> 'true'::jsonb
     OR p_object_literal->'unit' <> 'null'::jsonb
     OR (p_object_literal->>'approximate')::boolean IS TRUE THEN
    RAISE EXCEPTION 'death event is outside the deterministic rendering contract'
      USING ERRCODE='23514';
  END IF;
  role_value := p_subject_metadata->>'relationship_role';
  IF p_subject_entity_type='person'
     AND lower(p_subject_canonical_name) IN ('mother','father')
     AND (
       role_value IS NULL
       OR role_value='family:'||lower(p_subject_canonical_name)
     ) THEN
    result_value := format('The user''s %s died.',lower(p_subject_canonical_name));
  ELSE
    result_value := format('%s died.',p_subject_canonical_name);
  END IF;
  IF btrim(result_value) <> result_value
     OR result_value = ''
     OR length(result_value) > 2000
     OR result_value ~ '[[:cntrl:]]' THEN
    RAISE EXCEPTION 'rendered death-event claim is invalid'
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
  predicate_value text;
  matched integer;
BEGIN
  actor := memory.require_v5_writer_context();
  SELECT stored.predicate INTO predicate_value
  FROM memory.observation AS stored
  WHERE stored.owner_user_id=actor
    AND stored.observation_id=p_observation_id;
  IF predicate_value IS DISTINCT FROM 'life_event.died' THEN
    RETURN QUERY SELECT *
    FROM memory.preflight_claim_projection_source_v5_1_base(p_observation_id);
    RETURN;
  END IF;

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
    actual_contract.object_kind,
    observation.object_literal,
    memory.v5_digest_text(memory.v5_canonical_json_text(
      observation.object_literal
    )),
    binding.subject_entity_id,
    subject_entity.entity_type,
    subject_entity.status::text,
    subject_entity.canonical_name,
    NULL::uuid,
    NULL::text,
    NULL::text,
    NULL::text,
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
    memory.render_life_event_claim_text_v5_1(
      subject_entity.entity_type,
      subject_entity.canonical_name,
      subject_entity.metadata,
      observation.predicate,
      observation.object_literal
    )
  FROM memory.observation AS observation
  JOIN memory.predicate_contract AS actual_contract
    ON actual_contract.predicate=observation.predicate
   AND actual_contract.registry_version=observation.predicate_registry_version
  JOIN memory.predicate_contract AS governed_contract
    ON governed_contract.predicate=observation.predicate
   AND governed_contract.registry_version='memory_predicate_registry_v5_1'
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
  WHERE observation.owner_user_id=actor
    AND observation.observation_id=p_observation_id
    AND observation.predicate='life_event.died'
    AND observation.predicate_registry_version IN (
      'memory_predicate_registry_v5','memory_predicate_registry_v5_1'
    )
    AND actual_contract.lifecycle='active'
    AND actual_contract.extraction_allowed
    AND actual_contract.object_kind='literal'
    AND governed_contract.lifecycle='active'
    AND governed_contract.extraction_allowed
    AND governed_contract.object_kind='literal'
    AND governed_contract.contract->>'object_contract'='literal.true'
    AND observation.polarity='affirmed'
    AND observation.modality IN ('asserted','reported_observation')
    AND (governed_contract.contract->'modalities')
          ? observation.modality::text
    AND observation.projection_class IN ('direct_claim','supportive_context')
    AND (governed_contract.contract->'projection_classes')
          ? observation.projection_class::text
    AND (governed_contract.contract->'surface_policies')
          ? observation.surface_policy::text
    AND observation.sensitivity='high'
    AND memory.v5_project_scope_valid(observation.project_scope)
    AND observation.project_scope->>'state'='not_applicable'
    AND observation.object_mention_id IS NULL
    AND observation.object_literal IS NOT NULL
    AND binding.object_entity_id IS NULL
    AND memory.v5_literal_object_valid(observation.object_literal)
    AND observation.object_literal->>'datatype'='boolean'
    AND observation.object_literal->'value'='true'::jsonb
    AND observation.object_literal->'unit'='null'::jsonb
    AND (observation.object_literal->>'approximate')::boolean IS FALSE
    AND subject_entity.status='active'
    AND subject_entity.entity_type IN ('person','animal')
    AND (governed_contract.contract->'subject_entity_types')
          ? subject_entity.entity_type
    AND temporal.semantic='occurrence'
    AND (governed_contract.contract->'temporal_semantics')
          ? temporal.semantic::text
    AND evidence.status='active'
    AND evidence.content IS NOT NULL
    AND memory.v5_digest_text(evidence.content)=evidence.content_sha256
    AND memory.observation_entailment_allows_projection_v5(
      observation.observation_id,observation.observation_sha256
    );
  GET DIAGNOSTICS matched=ROW_COUNT;
  IF matched<>1 THEN
    RAISE EXCEPTION 'complete accepted owner-scoped death-event source not found'
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
    RETURN QUERY SELECT *
    FROM memory.preflight_claim_projection_packet_v5_1_base(
      p_plan_id,p_packet_text
    );
    RETURN;
  END;
  projection:=packet->'projections'->0;
  identity:=projection->'identity';
  IF identity->>'predicate' IS DISTINCT FROM 'life_event.died' THEN
    RETURN QUERY SELECT *
    FROM memory.preflight_claim_projection_packet_v5_1_base(
      p_plan_id,p_packet_text
    );
    RETURN;
  END IF;
  payload:=projection->'payload';
  observation_input:=projection->'observation_inputs'->0;
  IF NOT memory.v5_jsonb_exact_keys(packet,ARRAY[
       'contract_version','predicate_registry_version',
       'projection_policy_version','projector','projector_version',
       'projections','packet_sha256'
     ])
     OR packet->>'contract_version'<>'memory_v1_projection_plan_v5'
     OR packet->>'predicate_registry_version' NOT IN (
       'memory_predicate_registry_v5','memory_predicate_registry_v5_1'
     )
     OR packet->>'projection_policy_version'<>'memory_projection_policy_v5'
     OR packet->>'projector'<>
          'memory_v1_deterministic_life_event_claim_projection_v5_1'
     OR packet->>'projector_version'<>'life_event_template_v1'
     OR jsonb_typeof(packet->'projections')<>'array'
     OR jsonb_array_length(packet->'projections')<>1
     OR NOT memory.v5_jsonb_exact_keys(projection,ARRAY[
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
     OR identity->>'predicate'<>'life_event.died'
     OR identity->>'object_kind'<>'literal'
     OR identity->'object_entity_id'<>'null'::jsonb
     OR NOT memory.v5_sha256_valid(identity->>'object_literal_sha256')
     OR identity->>'polarity'<>'affirmed'
     OR identity->>'modality' NOT IN ('asserted','reported_observation')
     OR projection#>>'{target,action}'<>'create'
     OR projection#>'{target,aggregate_id}'<>'null'::jsonb
     OR projection#>'{target,expected_revision_number}'<>'null'::jsonb
     OR projection#>'{target,reason_codes}'<>'[]'::jsonb
     OR projection#>>'{temporal_policy,canonical_source}'<>
          'memory.observation_temporal'
     OR projection#>>'{temporal_policy,materialization}'<>'link_only'
     OR projection#>'{temporal_policy,source_observation_id}'<>'null'::jsonb
     OR projection#>>'{review,state}'<>'manual_review_required'
     OR (projection#>>'{review,authorization_required}')::boolean IS NOT TRUE
     OR projection#>'{review,reason_codes}'<>
          '["initial_life_event_claim_projection_requires_review"]'::jsonb
     OR projection->'relations'<>'[]'::jsonb
     OR payload->>'kind'<>'claim'
     OR payload->>'claim_class' NOT IN ('direct_claim','supportive_context')
     OR payload->>'surface_policy' NOT IN (
       'direct_or_relevant','mention_when_directly_relevant'
     )
     OR btrim(COALESCE(payload->>'canonical_text',''))=''
     OR length(payload->>'canonical_text')>2000 THEN
    RAISE EXCEPTION 'death-event claim body is outside the V5.1 contract'
      USING ERRCODE='23514';
  END IF;
  input_literal_sha:=identity->>'object_literal_sha256';
  SELECT * INTO source
  FROM memory.preflight_claim_projection_source_v5_1(
    (observation_input->>'observation_id')::uuid
  );
  IF source.observation_sha256<>observation_input->>'observation_sha256'
     OR source.predicate_registry_version<>packet->>'predicate_registry_version'
     OR source.subject_entity_id<>(identity->>'subject_entity_id')::uuid
     OR source.predicate<>identity->>'predicate'
     OR source.object_kind<>'literal'
     OR source.object_entity_id IS NOT NULL
     OR source.object_literal_sha256<>input_literal_sha
     OR source.polarity<>identity->>'polarity'
     OR source.modality<>identity->>'modality'
     OR source.projection_class<>payload->>'claim_class'
     OR source.surface_policy<>payload->>'surface_policy'
     OR source.canonical_text<>payload->>'canonical_text' THEN
    RAISE EXCEPTION 'death-event claim source binding is stale or forged'
      USING ERRCODE='23514';
  END IF;
  semantic_value:=memory.v5_projection_semantic_key_sha256(
    actor,'claim',(identity->>'subject_entity_id')::uuid,
    'life_event.died','literal',NULL,input_literal_sha,
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
    RAISE EXCEPTION 'death-event claim packet hash mismatch'
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

ALTER FUNCTION memory.render_life_event_claim_text_v5_1(
  text,text,jsonb,text,jsonb
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_claim_projection_source_v5_1(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_claim_projection_packet_v5_1(uuid,text)
  OWNER TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.preflight_claim_projection_source_v5_1_base(uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_claim_projection_packet_v5_1_base(
  uuid,text
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.render_life_event_claim_text_v5_1(
  text,text,jsonb,text,jsonb
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_claim_projection_source_v5_1(uuid)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.preflight_claim_projection_packet_v5_1(uuid,text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.preflight_claim_projection_source_v5_1(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_claim_projection_packet_v5_1(uuid,text)
  TO brains_app;

COMMIT;
