BEGIN;

DO $prerequisite$
BEGIN
  IF current_user <> 'sage'
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.preflight_projection_source_v5_2(uuid)') IS NULL
     OR to_regprocedure('memory.expected_projection_payload_v5_2(uuid)') IS NULL
     OR to_regclass('memory.observation_entailment_v5') IS NULL
     OR to_regclass('memory.projection_plan') IS NULL
     OR to_regclass('memory.projection_plan_item') IS NULL
     OR to_regclass('memory.projection_plan_observation') IS NULL
     OR to_regclass('memory.projection_claim_payload') IS NULL
     OR NOT EXISTS (
       SELECT 1
       FROM pg_roles
       WHERE rolname = 'memory_v5_writer'
         AND NOT rolcanlogin
         AND NOT rolsuper
         AND NOT rolcreatedb
         AND NOT rolcreaterole
         AND NOT rolinherit
         AND NOT rolbypassrls
     ) THEN
    RAISE EXCEPTION 'V5.2 reconciled stance projection prerequisites are missing';
  END IF;
END
$prerequisite$;

DO $standard_dispatch_v2_compatibility$
DECLARE
  renderer_definition text;
  preflight_definition text;
  renderer_sha256 text;
  preflight_sha256 text;
  old_renderer constant text := $old$
    ELSIF source.predicate='stance.reported' THEN
      result_value := subject_label||' reports the position that '||
        (source.object_literal::jsonb#>>'{value,position}')||'.';
$old$;
  new_renderer constant text := $new$
    ELSIF source.predicate='stance.reported' THEN
      result_value := subject_label||' reports this position: "'||
        regexp_replace(
          btrim(source.object_literal::jsonb#>>'{value,position}'),
          '[[:space:]]+',' ','g'
        )||
        CASE WHEN right(regexp_replace(
          btrim(source.object_literal::jsonb#>>'{value,position}'),
          '[[:space:]]+',' ','g'
        ),1) ~ '[.!?]' THEN '' ELSE '.' END||'"';
$new$;
  old_preflight constant text :=
    $old$     OR packet->>'projector_version'<>'semantic_dispatch_v1'$old$;
  new_preflight constant text :=
    $new$     OR packet->>'projector_version' NOT IN (
       'semantic_dispatch_v1','semantic_dispatch_v2'
     )$new$;
BEGIN
  SELECT pg_get_functiondef(
    'memory.render_projection_claim_text_v5_2(uuid)'::regprocedure
  )
  INTO renderer_definition;
  SELECT pg_get_functiondef(
    'memory.preflight_projection_packet_v5_2(uuid,text)'::regprocedure
  )
  INTO preflight_definition;
  renderer_sha256 := encode(public.digest(
    convert_to(renderer_definition, 'UTF8'), 'sha256'
  ), 'hex');
  preflight_sha256 := encode(public.digest(
    convert_to(preflight_definition, 'UTF8'), 'sha256'
  ), 'hex');
  IF renderer_sha256
       <> 'b9ac8b8d7277026fa436bf9fabc54444845437ef3a81f6ec3bae51d160b8d90e'
     OR preflight_sha256
       <> 'c315d4f1a991e6c1c69265a3d97429eb4f18d94e2f09dbb096db99a4328f39f0'
     OR length(renderer_definition)
          - length(replace(renderer_definition, old_renderer, ''))
          <> length(old_renderer)
     OR length(preflight_definition)
          - length(replace(preflight_definition, old_preflight, ''))
          <> length(old_preflight) THEN
    RAISE EXCEPTION 'standard V5.2 dispatch definition drifted';
  END IF;
  EXECUTE replace(renderer_definition, old_renderer, new_renderer);
  EXECUTE replace(preflight_definition, old_preflight, new_preflight);
END
$standard_dispatch_v2_compatibility$;

CREATE OR REPLACE FUNCTION memory.render_reconciled_stance_claim_text_v5_2(
  p_primary_observation_id uuid
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  source record;
  position_text text;
BEGIN
  SELECT *
  INTO STRICT source
  FROM memory.preflight_projection_source_v5_2(p_primary_observation_id);

  IF source.predicate <> 'stance.reported'
     OR source.subject_entity_type <> 'self'
     OR source.object_kind <> 'literal'
     OR source.polarity <> 'affirmed'
     OR source.modality <> 'reported_belief'
     OR source.projection_class <> 'reported_stance'
     OR source.surface_policy <> 'relevant_recall_or_explicit_recall'
     OR source.object_literal #>> '{value,position}' IS NULL THEN
    RAISE EXCEPTION 'primary observation is not an eligible reported stance'
      USING ERRCODE = '23514';
  END IF;

  position_text := btrim(source.object_literal #>> '{value,position}');
  IF position_text = ''
     OR length(position_text) > 1500
     OR position_text ~ '[[:cntrl:]]' THEN
    RAISE EXCEPTION 'reported stance text is invalid'
      USING ERRCODE = '23514';
  END IF;
  IF right(position_text, 1) !~ '[.!?]' THEN
    position_text := position_text || '.';
  END IF;
  RETURN 'The user reports this position: "' || position_text || '"';
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_reconciled_stance_projection_v5_2(
  p_plan_id uuid,
  p_packet_text text
)
RETURNS TABLE(
  packet_text_sha256 text,
  semantic_key_sha256 text,
  projection_sha256 text,
  packet_sha256 text,
  owner_manifest_sha256 text,
  primary_observation_id uuid,
  context_observation_id uuid,
  existing_aggregates integer,
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
  primary_input jsonb;
  context_input jsonb;
  primary_source record;
  context_source record;
  expected record;
  semantic_value text;
  projection_hash text;
  packet_hash text;
  aggregate_count integer;
  plan_count integer;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_plan_id IS NULL OR btrim(COALESCE(p_packet_text, '')) = '' THEN
    RAISE EXCEPTION 'V5.2 reconciled projection identifiers are required';
  END IF;
  BEGIN
    packet := p_packet_text::jsonb;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'V5.2 reconciled projection packet is not valid JSON';
  END;

  IF NOT memory.v5_jsonb_exact_keys(packet, ARRAY[
       'contract_version', 'predicate_registry_version',
       'projection_policy_version', 'projector', 'projector_version',
       'projections', 'packet_sha256'
     ])
     OR packet->>'contract_version' <> 'memory_v1_projection_plan_v5'
     OR packet->>'predicate_registry_version' <> 'memory_predicate_registry_v5_2'
     OR packet->>'projection_policy_version' <> 'memory_projection_policy_v5'
     OR packet->>'projector' <> 'memory_v1_deterministic_projection_v5_2'
     OR packet->>'projector_version' <> 'stance_reconciliation_v1'
     OR jsonb_typeof(packet->'projections') <> 'array'
     OR jsonb_array_length(packet->'projections') <> 1 THEN
    RAISE EXCEPTION 'V5.2 reconciled projection envelope mismatch';
  END IF;

  projection := packet->'projections'->0;
  identity := projection->'identity';
  payload := projection->'payload';
  IF NOT memory.v5_jsonb_exact_keys(projection, ARRAY[
       'projection_ref', 'lane', 'observation_inputs', 'identity', 'target',
       'temporal_policy', 'review', 'relations', 'payload'
     ])
     OR NOT memory.v5_jsonb_exact_keys(identity, ARRAY[
       'subject_entity_id', 'predicate', 'object_kind', 'object_entity_id',
       'object_literal_sha256', 'polarity', 'modality', 'semantic_key_sha256'
     ])
     OR NOT memory.v5_jsonb_exact_keys(projection->'target', ARRAY[
       'action', 'aggregate_id', 'expected_revision_number', 'reason_codes'
     ])
     OR NOT memory.v5_jsonb_exact_keys(projection->'temporal_policy', ARRAY[
       'canonical_source', 'materialization', 'source_observation_id'
     ])
     OR NOT memory.v5_jsonb_exact_keys(projection->'review', ARRAY[
       'state', 'authorization_required', 'reason_codes'
     ])
     OR NOT memory.v5_jsonb_exact_keys(payload, ARRAY[
       'kind', 'claim_class', 'canonical_text', 'surface_policy'
     ])
     OR projection->>'projection_ref' <> 'p01'
     OR projection->>'lane' <> 'claim'
     OR jsonb_typeof(projection->'observation_inputs') <> 'array'
     OR jsonb_array_length(projection->'observation_inputs') <> 2
     OR projection#>>'{observation_inputs,0,stance}' <> 'supports'
     OR projection#>>'{observation_inputs,1,stance}' <> 'context'
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
          <> '["initial_v5_2_reconciled_stance_requires_review"]'::jsonb
     OR projection->'relations' <> '[]'::jsonb
     OR payload->>'kind' <> 'claim'
     OR payload->>'claim_class' <> 'reported_stance'
     OR payload->>'surface_policy' <> 'relevant_recall_or_explicit_recall' THEN
    RAISE EXCEPTION 'V5.2 reconciled projection body mismatch';
  END IF;

  primary_input := projection->'observation_inputs'->0;
  context_input := projection->'observation_inputs'->1;
  IF NOT memory.v5_jsonb_exact_keys(primary_input, ARRAY[
       'observation_id', 'observation_sha256', 'stance'
     ])
     OR NOT memory.v5_jsonb_exact_keys(context_input, ARRAY[
       'observation_id', 'observation_sha256', 'stance'
     ])
     OR primary_input->>'observation_id' = context_input->>'observation_id' THEN
    RAISE EXCEPTION 'V5.2 reconciled observation inputs mismatch';
  END IF;

  SELECT *
  INTO STRICT primary_source
  FROM memory.preflight_projection_source_v5_2(
    (primary_input->>'observation_id')::uuid
  );
  SELECT *
  INTO STRICT context_source
  FROM memory.preflight_projection_source_v5_2(
    (context_input->>'observation_id')::uuid
  );
  SELECT *
  INTO STRICT expected
  FROM memory.expected_projection_payload_v5_2(primary_source.observation_id);

  IF primary_input->>'observation_sha256' <> primary_source.observation_sha256
     OR context_input->>'observation_sha256' <> context_source.observation_sha256
     OR primary_source.evidence_id <> context_source.evidence_id
     OR primary_source.subject_entity_id <> context_source.subject_entity_id
     OR primary_source.predicate <> 'stance.reported'
     OR context_source.predicate <> 'stance.reported'
     OR primary_source.subject_entity_type <> 'self'
     OR context_source.subject_entity_type <> 'self'
     OR primary_source.object_kind <> 'literal'
     OR context_source.object_kind <> 'literal'
     OR primary_source.polarity <> 'affirmed'
     OR context_source.polarity <> 'affirmed'
     OR primary_source.modality <> 'reported_belief'
     OR context_source.modality <> 'reported_belief'
     OR primary_source.projection_class <> 'reported_stance'
     OR context_source.projection_class <> 'reported_stance'
     OR primary_source.surface_policy <> 'relevant_recall_or_explicit_recall'
     OR context_source.surface_policy <> 'relevant_recall_or_explicit_recall'
     OR expected.lane <> 'claim'
     OR NOT EXISTS (
       SELECT 1
       FROM memory.observation_entailment_v5 AS entailment
       WHERE entailment.owner_user_id = actor
         AND entailment.observation_id = primary_source.observation_id
         AND entailment.observation_sha256 = primary_source.observation_sha256
         AND entailment.decision = 'accepted'
     )
     OR NOT EXISTS (
       SELECT 1
       FROM memory.observation_entailment_v5 AS entailment
       WHERE entailment.owner_user_id = actor
         AND entailment.observation_id = context_source.observation_id
         AND entailment.observation_sha256 = context_source.observation_sha256
         AND entailment.decision = 'accepted'
     ) THEN
    RAISE EXCEPTION 'V5.2 reconciled sources are not compatible accepted stances';
  END IF;

  IF payload->>'canonical_text'
       <> memory.render_reconciled_stance_claim_text_v5_2(
            primary_source.observation_id
          )
     OR identity->>'subject_entity_id' <> primary_source.subject_entity_id::text
     OR identity->>'predicate' <> primary_source.predicate
     OR identity->>'object_kind' <> primary_source.object_kind
     OR NULLIF(identity->>'object_entity_id', '')
          IS DISTINCT FROM primary_source.object_entity_id::text
     OR NULLIF(identity->>'object_literal_sha256', '')
          IS DISTINCT FROM primary_source.object_literal_sha256
     OR identity->>'polarity' <> primary_source.polarity
     OR identity->>'modality' <> primary_source.modality THEN
    RAISE EXCEPTION 'V5.2 reconciled projection is not primary-source exact';
  END IF;

  semantic_value := memory.v5_projection_semantic_key_sha256(
    actor, expected.lane, primary_source.subject_entity_id,
    primary_source.predicate, primary_source.object_kind,
    primary_source.object_entity_id, primary_source.object_literal_sha256,
    primary_source.polarity::memory.observation_polarity,
    primary_source.modality::memory.observation_modality,
    expected.lane_scope
  );
  projection_hash := memory.v5_digest_text(
    memory.v5_canonical_json_text(projection)
  );
  packet_hash := memory.v5_digest_text(
    memory.v5_canonical_json_text(packet - 'packet_sha256')
  );
  IF identity->>'semantic_key_sha256' <> semantic_value
     OR packet->>'packet_sha256' <> packet_hash THEN
    RAISE EXCEPTION 'V5.2 reconciled projection hash mismatch';
  END IF;

  SELECT count(*)
  INTO aggregate_count
  FROM memory.claim AS aggregate
  WHERE aggregate.owner_user_id = actor
    AND aggregate.canonical_key = 'v5:' || semantic_value;
  SELECT count(*)
  INTO plan_count
  FROM memory.projection_plan AS stored_plan
  WHERE stored_plan.owner_user_id = actor
    AND (
      stored_plan.plan_id = p_plan_id
      OR stored_plan.packet_sha256 = packet_hash
    );

  RETURN QUERY SELECT
    memory.v5_digest_text(p_packet_text),
    semantic_value,
    projection_hash,
    packet_hash,
    memory.v5_projection_owner_manifest_sha256(actor, packet_hash),
    primary_source.observation_id,
    context_source.observation_id,
    aggregate_count,
    plan_count;
END
$function$;

CREATE OR REPLACE FUNCTION memory.stage_reconciled_stance_projection_v5_2(
  p_plan_id uuid,
  p_packet_text text,
  p_expected_owner_manifest_sha256 text
)
RETURNS TABLE(plan_id uuid, outcome text, rows_written integer, result jsonb)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  packet jsonb;
  projection_value jsonb;
  identity jsonb;
  payload jsonb;
  primary_input jsonb;
  expected record;
  preflight record;
  existing memory.projection_plan%ROWTYPE;
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_plan_id IS NULL
     OR NOT memory.v5_sha256_valid(p_expected_owner_manifest_sha256) THEN
    RAISE EXCEPTION 'V5.2 reconciled stage identifiers are invalid';
  END IF;
  packet := p_packet_text::jsonb;
  projection_value := packet->'projections'->0;
  identity := projection_value->'identity';
  payload := projection_value->'payload';
  primary_input := projection_value->'observation_inputs'->0;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || '|projection-v5-2-reconciled|' || p_plan_id::text, 0
  ));
  SELECT stored.*
  INTO existing
  FROM memory.projection_plan AS stored
  WHERE stored.owner_user_id = actor
    AND (
      stored.plan_id = p_plan_id
      OR stored.packet_sha256 = packet->>'packet_sha256'
    );
  IF FOUND THEN
    IF existing.plan_id <> p_plan_id
       OR existing.packet_text <> p_packet_text
       OR existing.owner_manifest_sha256 <> p_expected_owner_manifest_sha256
       OR NOT EXISTS (
         SELECT 1
         FROM memory.projection_plan_item AS item
         WHERE item.owner_user_id = actor
           AND item.plan_id = p_plan_id
           AND item.projection_ref = 'p01'
           AND item.projection = projection_value
       )
       OR (
         SELECT count(*)
         FROM memory.projection_plan_observation AS link
         WHERE link.owner_user_id = actor
           AND link.plan_id = p_plan_id
           AND link.projection_ref = 'p01'
       ) <> 2
       OR EXISTS (
         SELECT 1
         FROM jsonb_array_elements(
           projection_value->'observation_inputs'
         ) AS input(value)
         WHERE NOT EXISTS (
           SELECT 1
           FROM memory.projection_plan_observation AS link
           WHERE link.owner_user_id = actor
             AND link.plan_id = p_plan_id
             AND link.projection_ref = 'p01'
             AND link.observation_id =
                   (input.value->>'observation_id')::uuid
             AND link.observation_sha256 =
                   input.value->>'observation_sha256'
             AND link.stance::text = input.value->>'stance'
         )
       ) THEN
      RAISE EXCEPTION 'V5.2 reconciled projection replay state mismatch';
    END IF;
    RETURN QUERY SELECT p_plan_id, 'replayed', 0, jsonb_build_object(
      'plan_id', p_plan_id,
      'projection_ref', 'p01',
      'packet_sha256', existing.packet_sha256,
      'observation_count', 2
    );
    RETURN;
  END IF;

  SELECT *
  INTO STRICT preflight
  FROM memory.preflight_reconciled_stance_projection_v5_2(
    p_plan_id, p_packet_text
  );
  IF preflight.existing_aggregates <> 0
     OR preflight.existing_plans <> 0
     OR preflight.owner_manifest_sha256
          <> p_expected_owner_manifest_sha256 THEN
    RAISE EXCEPTION 'V5.2 reconciled projection preflight is stale or mismatched';
  END IF;
  SELECT *
  INTO STRICT expected
  FROM memory.expected_projection_payload_v5_2(
    (primary_input->>'observation_id')::uuid
  );

  INSERT INTO memory.projection_plan(
    owner_user_id, plan_id, contract_version,
    predicate_registry_version, projection_policy_version,
    projector, projector_version, packet_text, packet_text_sha256,
    packet_sha256, owner_manifest_sha256, projection_count,
    invoked_by_session
  ) VALUES (
    actor, p_plan_id, packet->>'contract_version',
    packet->>'predicate_registry_version',
    packet->>'projection_policy_version',
    packet->>'projector', packet->>'projector_version',
    p_packet_text, preflight.packet_text_sha256,
    preflight.packet_sha256, preflight.owner_manifest_sha256,
    1, session_user
  );

  INSERT INTO memory.projection_plan_item(
    owner_user_id, plan_id, projection_ref,
    predicate_registry_version, lane, projection, projection_sha256,
    subject_entity_id, predicate, object_kind, object_entity_id,
    object_literal_sha256, polarity, modality, lane_scope,
    semantic_key_sha256, target_action, expected_revision_number,
    target_reason_codes, temporal_materialization,
    temporal_source_observation_id, review_state,
    authorization_required, review_reason_codes
  ) VALUES (
    actor, p_plan_id, 'p01', 'memory_predicate_registry_v5_2',
    'claim', projection_value, preflight.projection_sha256,
    (identity->>'subject_entity_id')::uuid,
    identity->>'predicate', identity->>'object_kind',
    NULLIF(identity->>'object_entity_id', '')::uuid,
    NULLIF(identity->>'object_literal_sha256', ''),
    (identity->>'polarity')::memory.observation_polarity,
    (identity->>'modality')::memory.observation_modality,
    expected.lane_scope, preflight.semantic_key_sha256,
    'create', NULL, '[]'::jsonb, 'link_only', NULL,
    'manual_review_required', true,
    projection_value#>'{review,reason_codes}'
  );

  INSERT INTO memory.projection_claim_payload(
    owner_user_id, plan_id, projection_ref, target_action,
    target_claim_id, claim_class, canonical_text, surface_policy
  ) VALUES (
    actor, p_plan_id, 'p01', 'create', NULL,
    payload->>'claim_class', payload->>'canonical_text',
    (payload->>'surface_policy')::memory.observation_surface_policy
  );

  INSERT INTO memory.projection_plan_observation(
    owner_user_id, plan_id, projection_ref,
    observation_id, observation_sha256, stance
  )
  SELECT
    actor, p_plan_id, 'p01',
    (input.value->>'observation_id')::uuid,
    input.value->>'observation_sha256',
    (input.value->>'stance')::memory.projection_observation_stance_v5
  FROM jsonb_array_elements(
    projection_value->'observation_inputs'
  ) WITH ORDINALITY AS input(value, ordinality)
  ORDER BY input.ordinality;

  SET CONSTRAINTS ALL IMMEDIATE;
  result_value := jsonb_build_object(
    'plan_id', p_plan_id,
    'projection_ref', 'p01',
    'packet_sha256', preflight.packet_sha256,
    'lane', 'claim',
    'primary_observation_id', preflight.primary_observation_id,
    'context_observation_id', preflight.context_observation_id,
    'observation_count', 2
  );
  RETURN QUERY SELECT p_plan_id, 'applied', 5, result_value;
END
$function$;

DO $guard_compatibility$
DECLARE
  definition text;
  expected_definition_sha256 constant text :=
    'd2ac3081eb9c10fe9a7d641888964b65a4c12366d5dcec774aadf779402aaec3';
  actual_definition_sha256 text;
  old_fragment constant text := $old$
        OR (
          item.object_kind = 'literal'
          AND memory.v5_digest_text(memory.v5_canonical_json_text(
            observation.object_literal
          )) <> item.object_literal_sha256
        )
$old$;
  new_fragment constant text := $new$
        OR (
          item.object_kind = 'literal'
          AND memory.v5_digest_text(memory.v5_canonical_json_text(
            observation.object_literal
          )) <> item.object_literal_sha256
          AND NOT (
            link.stance = 'context'
            AND item.predicate = 'stance.reported'
            AND EXISTS (
              SELECT 1
              FROM memory.projection_plan AS plan
              WHERE plan.owner_user_id = item.owner_user_id
                AND plan.plan_id = item.plan_id
                AND plan.projector =
                      'memory_v1_deterministic_projection_v5_2'
                AND plan.projector_version = 'stance_reconciliation_v1'
            )
          )
        )
$new$;
BEGIN
  SELECT pg_get_functiondef(
    'memory.guard_projection_item_complete_v5()'::regprocedure
  )
  INTO definition;
  actual_definition_sha256 := encode(public.digest(
    convert_to(definition, 'UTF8'), 'sha256'
  ), 'hex');
  IF actual_definition_sha256 <> expected_definition_sha256
     OR length(definition) - length(replace(definition, old_fragment, ''))
          <> length(old_fragment) THEN
    RAISE EXCEPTION 'projection completeness guard definition drifted';
  END IF;
  EXECUTE replace(definition, old_fragment, new_fragment);
END
$guard_compatibility$;

ALTER FUNCTION memory.render_reconciled_stance_claim_text_v5_2(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_reconciled_stance_projection_v5_2(uuid, text)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.stage_reconciled_stance_projection_v5_2(
  uuid, text, text
) OWNER TO memory_v5_writer;

REVOKE ALL ON FUNCTION
  memory.render_reconciled_stance_claim_text_v5_2(uuid)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION
  memory.preflight_reconciled_stance_projection_v5_2(uuid, text)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION
  memory.stage_reconciled_stance_projection_v5_2(uuid, text, text)
  FROM PUBLIC, brains_app;

GRANT EXECUTE ON FUNCTION
  memory.preflight_reconciled_stance_projection_v5_2(uuid, text)
  TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.stage_reconciled_stance_projection_v5_2(uuid, text, text)
  TO brains_app;

COMMIT;
