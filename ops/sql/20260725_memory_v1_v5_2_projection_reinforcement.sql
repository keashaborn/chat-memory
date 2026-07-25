BEGIN;

DO $prerequisite$
BEGIN
  IF current_user <> 'sage'
     OR to_regprocedure(
       'memory.preflight_projection_packet_v5_2(uuid,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.stage_projection_plan_v5_2(uuid,text,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.apply_projection_v5(uuid,uuid,text,uuid,text)'
     ) IS NULL
     OR to_regclass('memory.claim') IS NULL
     OR to_regclass('memory.claim_revision') IS NULL
     OR to_regclass('memory.claim_observation') IS NULL THEN
    RAISE EXCEPTION 'V5.2 projection reinforcement prerequisites are missing';
  END IF;
END
$prerequisite$;

CREATE OR REPLACE FUNCTION
  memory.v5_2_canonical_name_reinforcement_policy_bridge(
    p_owner_user_id uuid,
    p_plan_id uuid,
    p_projection_ref text,
    p_observation_id uuid
  )
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path=''
AS $function$
  SELECT EXISTS (
    SELECT 1
    FROM memory.projection_plan_item AS item
    JOIN memory.projection_claim_payload AS payload
      ON payload.owner_user_id=item.owner_user_id
     AND payload.plan_id=item.plan_id
     AND payload.projection_ref=item.projection_ref
    JOIN memory.projection_plan_observation AS link
      ON link.owner_user_id=item.owner_user_id
     AND link.plan_id=item.plan_id
     AND link.projection_ref=item.projection_ref
    JOIN memory.observation AS observation
      ON observation.owner_user_id=link.owner_user_id
     AND observation.observation_id=link.observation_id
    JOIN memory.observation_entity_binding AS binding
      ON binding.owner_user_id=observation.owner_user_id
     AND binding.observation_id=observation.observation_id
    JOIN memory.claim AS target
      ON target.owner_user_id=payload.owner_user_id
     AND target.claim_id=payload.target_claim_id
    WHERE item.owner_user_id=p_owner_user_id
      AND item.plan_id=p_plan_id
      AND item.projection_ref=p_projection_ref
      AND link.observation_id=p_observation_id
      AND item.lane='claim'
      AND item.target_action='reinforce'
      AND item.target_reason_codes='[
        "additional_supporting_observation",
        "canonical_name_correction_normalized"
      ]'::jsonb
      AND item.review_reason_codes=
        '["v5_2_canonical_name_reinforcement_requires_review"]'::jsonb
      AND item.predicate_registry_version=
        'memory_predicate_registry_v5_2'
      AND item.predicate='identity.name_canonical'
      AND item.object_kind='literal'
      AND item.object_entity_id IS NULL
      AND item.polarity='affirmed'
      AND item.modality='corrective'
      AND payload.target_action='reinforce'
      AND payload.claim_class='direct_claim'
      AND payload.surface_policy='direct_or_relevant'
      AND payload.canonical_text=target.canonical_text
      AND observation.predicate_registry_version=
        'memory_predicate_registry_v5_2'
      AND observation.predicate='identity.name_canonical'
      AND observation.polarity='affirmed'
      AND observation.modality='corrective'
      AND observation.projection_class='correction'
      AND observation.surface_policy='normalization_only'
      AND binding.subject_entity_id=item.subject_entity_id
      AND binding.object_entity_id IS NULL
      AND memory.v5_digest_text(memory.v5_canonical_json_text(
            observation.object_literal
          ))=item.object_literal_sha256
      AND target.status='supported'
      AND target.canonical_key='v5:'||item.semantic_key_sha256
      AND target.subject_entity_id=item.subject_entity_id
      AND target.predicate=item.predicate
      AND target.object_entity_id IS NULL
      AND item.expected_revision_number=(
        SELECT max(revision.revision_number)
        FROM memory.claim_revision AS revision
        WHERE revision.owner_user_id=target.owner_user_id
          AND revision.claim_id=target.claim_id
      )
      AND target.retrieval_policy->>'surface_policy'='direct_or_relevant'
      AND lower(btrim(target.object_literal->>'value'))=
            lower(btrim(observation.object_literal->>'value'))
  )
$function$;

DO $install_policy_bridge$
DECLARE
  guard_definition text;
  marker text :=
    E'      -- canonical_name_claim_source_v5_2_compat\n'
    || E'      AND NOT (\n';
  replacement text :=
    E'      -- canonical_name_claim_source_v5_2_compat\n'
    || E'      AND NOT (\n'
    || E'        memory.v5_2_canonical_name_reinforcement_policy_bridge(\n'
    || E'          item.owner_user_id,item.plan_id,item.projection_ref,\n'
    || E'          link.observation_id\n'
    || E'        )\n'
    || E'        OR\n';
BEGIN
  SELECT pg_get_functiondef(
    'memory.guard_projection_item_complete_v5()'::regprocedure
  ) INTO guard_definition;
  IF strpos(
       guard_definition,
       'memory.v5_2_canonical_name_reinforcement_policy_bridge('
     )=0 THEN
    IF length(guard_definition)-length(replace(guard_definition,marker,''))
         <>length(marker) THEN
      RAISE EXCEPTION
        'V5.2 reinforcement policy-bridge guard source drifted';
    END IF;
    EXECUTE replace(guard_definition,marker,replacement);
  END IF;
END
$install_policy_bridge$;

CREATE OR REPLACE FUNCTION memory.preflight_projection_reinforcement_v5_2(
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
  existing_plans integer,
  target_claim_id uuid,
  target_revision_number integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  packet jsonb;
  projection jsonb;
  target jsonb;
  review jsonb;
  create_packet jsonb;
  create_projection jsonb;
  create_preflight record;
  source record;
  expected record;
  claim_row memory.claim%ROWTYPE;
  claim_revision_number integer;
  target_claim uuid;
  target_revision integer;
  packet_hash text;
  projection_hash text;
  plan_count integer;
  object_literal_sha text;
  semantic_value text;
  aggregate_count integer;
  normalization_mode boolean;
  expected_payload jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_plan_id IS NULL OR btrim(COALESCE(p_packet_text,''))='' THEN
    RAISE EXCEPTION 'V5.2 reinforcement identifiers are required';
  END IF;
  BEGIN
    packet := p_packet_text::jsonb;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'V5.2 reinforcement packet is not valid JSON';
  END;
  projection := packet->'projections'->0;
  target := projection->'target';
  review := projection->'review';
  normalization_mode :=
    target->'reason_codes' = '[
      "additional_supporting_observation",
      "canonical_name_correction_normalized"
    ]'::jsonb
    AND review->'reason_codes' =
      '["v5_2_canonical_name_reinforcement_requires_review"]'::jsonb;
  IF NOT memory.v5_jsonb_exact_keys(packet,ARRAY[
       'contract_version','predicate_registry_version',
       'projection_policy_version','projector','projector_version',
       'projections','packet_sha256'
     ])
     OR packet->>'contract_version'<>'memory_v1_projection_plan_v5'
     OR packet->>'predicate_registry_version'
          <>'memory_predicate_registry_v5_2'
     OR packet->>'projection_policy_version'
          <>'memory_projection_policy_v5'
     OR packet->>'projector'
          <>'memory_v1_deterministic_projection_v5_2'
     OR packet->>'projector_version'<>'semantic_dispatch_v2'
     OR jsonb_typeof(packet->'projections')<>'array'
     OR jsonb_array_length(packet->'projections')<>1
     OR NOT memory.v5_jsonb_exact_keys(projection,ARRAY[
       'projection_ref','lane','observation_inputs','identity','target',
       'temporal_policy','review','relations','payload'
     ])
     OR projection->>'projection_ref'<>'p01'
     OR jsonb_array_length(projection->'observation_inputs')<>1
     OR projection#>>'{observation_inputs,0,stance}'<>'supports'
     OR NOT memory.v5_jsonb_exact_keys(target,ARRAY[
       'action','aggregate_id','expected_revision_number','reason_codes'
     ])
     OR target->>'action'<>'reinforce'
     OR NOT memory.v5_jsonb_exact_keys(review,ARRAY[
       'state','authorization_required','reason_codes'
     ])
     OR review->>'state'<>'manual_review_required'
     OR (review->>'authorization_required')::boolean IS NOT TRUE
     OR NOT (
       normalization_mode
       OR (
         target->'reason_codes'
           = '["additional_supporting_observation"]'::jsonb
         AND review->'reason_codes'
           = '["v5_2_claim_reinforcement_requires_review"]'::jsonb
       )
     )
     OR projection->'relations'<>'[]'::jsonb
     OR projection->>'lane'<>'claim' THEN
    RAISE EXCEPTION 'V5.2 reinforcement policy mismatch';
  END IF;
  BEGIN
    target_claim := (target->>'aggregate_id')::uuid;
    target_revision := (target->>'expected_revision_number')::integer;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'V5.2 reinforcement target is invalid';
  END;
  IF target_claim IS NULL OR target_revision < 1 THEN
    RAISE EXCEPTION 'V5.2 reinforcement target is invalid';
  END IF;

  SELECT * INTO STRICT source
  FROM memory.preflight_projection_source_v5_2(
    (projection#>>'{observation_inputs,0,observation_id}')::uuid
  );
  SELECT * INTO STRICT expected
  FROM memory.expected_projection_payload_v5_2(source.observation_id);
  IF normalization_mode THEN
    IF source.predicate<>'identity.name_canonical'
       OR source.projection_class<>'correction'
       OR source.surface_policy<>'normalization_only'
       OR source.modality<>'corrective'
       OR source.polarity<>'affirmed'
       OR source.object_kind<>'literal'
       OR source.object_literal->>'datatype'<>'text'
       OR btrim(COALESCE(source.object_literal->>'value',''))=''
       OR projection#>>'{identity,subject_entity_id}'
            <>source.subject_entity_id::text
       OR projection#>>'{identity,predicate}'<>source.predicate
       OR projection#>>'{identity,object_kind}'<>'literal'
       OR projection#>'{identity,object_entity_id}'<>'null'::jsonb
       OR projection#>>'{identity,polarity}'<>source.polarity
       OR projection#>>'{identity,modality}'<>source.modality
       OR projection#>>'{observation_inputs,0,observation_id}'
            <>source.observation_id::text
       OR projection#>>'{observation_inputs,0,observation_sha256}'
            <>source.observation_sha256
       OR projection#>>'{temporal_policy,canonical_source}'
            <>'memory.observation_temporal'
       OR projection#>>'{temporal_policy,materialization}'
            <>expected.temporal_materialization::text
       OR NULLIF(
            projection#>>'{temporal_policy,source_observation_id}',''
          ) IS DISTINCT FROM expected.temporal_source_observation_id::text THEN
      RAISE EXCEPTION 'V5.2 canonical-name normalization source mismatch';
    END IF;
    semantic_value := memory.v5_projection_semantic_key_sha256(
      actor,'claim',source.subject_entity_id,source.predicate,'literal',
      NULL,source.object_literal_sha256,
      source.polarity::memory.observation_polarity,
      source.modality::memory.observation_modality,
      expected.lane_scope
    );
    expected_payload := jsonb_build_object(
      'kind','claim',
      'claim_class','direct_claim',
      'canonical_text',
        memory.render_projection_claim_text_v5_2(source.observation_id),
      'surface_policy','direct_or_relevant'
    );
    IF projection#>>'{identity,object_literal_sha256}'
          <>source.object_literal_sha256
       OR projection#>>'{identity,semantic_key_sha256}'<>semantic_value
       OR projection->'payload'<>expected_payload THEN
      RAISE EXCEPTION 'V5.2 canonical-name normalization target mismatch';
    END IF;
  ELSE
    -- Reuse the installed exact-source validator by substituting only the
    -- create-only policy fields.
    create_projection := jsonb_set(
      jsonb_set(
        projection,
        '{target}',
        jsonb_build_object(
          'action','create','aggregate_id',NULL,
          'expected_revision_number',NULL,'reason_codes','[]'::jsonb
        )
      ),
      '{review}',
      jsonb_build_object(
        'state','manual_review_required',
        'authorization_required',true,
        'reason_codes',
        '["initial_v5_2_projection_requires_review"]'::jsonb
      )
    );
    create_packet := jsonb_set(packet,'{projections,0}',create_projection);
    create_packet := jsonb_set(create_packet,'{packet_sha256}','""'::jsonb);
    create_packet := jsonb_set(
      create_packet,
      '{packet_sha256}',
      to_jsonb(memory.v5_digest_text(
        memory.v5_canonical_json_text(create_packet-'packet_sha256')
      ))
    );
    SELECT * INTO STRICT create_preflight
    FROM memory.preflight_projection_packet_v5_2(
      p_plan_id,memory.v5_canonical_json_text(create_packet)
    );
    semantic_value := create_preflight.semantic_key_sha256;
    expected_payload := expected.payload;
    IF expected.lane<>'claim'
       OR projection->'payload'<>expected_payload THEN
      RAISE EXCEPTION 'V5.2 reinforcement payload mismatch';
    END IF;
  END IF;

  SELECT count(*) INTO aggregate_count
  FROM memory.claim AS aggregate
  WHERE aggregate.owner_user_id=actor
    AND aggregate.canonical_key='v5:'||semantic_value;
  IF aggregate_count<>1 THEN
    RAISE EXCEPTION 'V5.2 reinforcement requires one existing semantic target';
  END IF;
  SELECT stored.* INTO claim_row
  FROM memory.claim AS stored
  WHERE stored.owner_user_id=actor
    AND stored.claim_id=target_claim;
  IF NOT FOUND
     OR claim_row.status<>'supported'
     OR claim_row.canonical_key
          <>'v5:'||semantic_value
     OR claim_row.subject_entity_id<>source.subject_entity_id
     OR claim_row.predicate<>source.predicate
     OR claim_row.object_entity_id IS DISTINCT FROM source.object_entity_id
     OR claim_row.canonical_text<>expected_payload->>'canonical_text'
     OR claim_row.retrieval_policy->>'surface_policy'
          IS DISTINCT FROM expected_payload->>'surface_policy' THEN
    RAISE EXCEPTION 'V5.2 reinforcement claim target mismatch';
  END IF;
  object_literal_sha := CASE
    WHEN claim_row.object_literal IS NULL THEN NULL
    ELSE memory.v5_digest_text(
      memory.v5_canonical_json_text(claim_row.object_literal)
    )
  END;
  IF (
       normalization_mode
       AND (
         claim_row.object_literal->>'kind'<>'literal'
         OR claim_row.object_literal->>'datatype'<>'text'
         OR lower(btrim(claim_row.object_literal->>'value'))
              <>lower(btrim(source.object_literal->>'value'))
       )
     )
     OR (
       NOT normalization_mode
       AND object_literal_sha IS DISTINCT FROM
         projection#>>'{identity,object_literal_sha256}'
     ) THEN
    RAISE EXCEPTION 'V5.2 reinforcement literal target mismatch';
  END IF;
  SELECT COALESCE(max(revision.revision_number),0)
  INTO claim_revision_number
  FROM memory.claim_revision AS revision
  WHERE revision.owner_user_id=actor
    AND revision.claim_id=target_claim;
  IF claim_revision_number<>target_revision THEN
    RAISE EXCEPTION 'V5.2 reinforcement optimistic revision mismatch'
      USING ERRCODE='40001';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.claim_observation AS link
    WHERE link.owner_user_id=actor
      AND link.claim_id=target_claim
      AND link.observation_id=source.observation_id
  ) THEN
    RAISE EXCEPTION 'V5.2 reinforcement observation is already linked'
      USING ERRCODE='23505';
  END IF;

  projection_hash := memory.v5_digest_text(
    memory.v5_canonical_json_text(projection)
  );
  packet_hash := memory.v5_digest_text(
    memory.v5_canonical_json_text(packet-'packet_sha256')
  );
  IF packet->>'packet_sha256'<>packet_hash THEN
    RAISE EXCEPTION 'V5.2 reinforcement packet hash mismatch';
  END IF;
  SELECT count(*) INTO plan_count
  FROM memory.projection_plan AS stored
  WHERE stored.owner_user_id=actor
    AND (stored.plan_id=p_plan_id OR stored.packet_sha256=packet_hash);
  RETURN QUERY SELECT
    memory.v5_digest_text(p_packet_text),
    semantic_value,
    projection_hash,
    packet_hash,
    memory.v5_projection_owner_manifest_sha256(actor,packet_hash),
    1,
    plan_count,
    target_claim,
    target_revision;
END
$function$;

CREATE OR REPLACE FUNCTION memory.stage_projection_reinforcement_v5_2(
  p_plan_id uuid,
  p_packet_text text,
  p_expected_owner_manifest_sha256 text
)
RETURNS TABLE(plan_id uuid,outcome text,rows_written integer,result jsonb)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  packet jsonb;
  projection_value jsonb;
  identity jsonb;
  payload jsonb;
  input jsonb;
  target jsonb;
  expected record;
  preflight record;
  existing memory.projection_plan%ROWTYPE;
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_plan_id IS NULL
     OR NOT memory.v5_sha256_valid(p_expected_owner_manifest_sha256) THEN
    RAISE EXCEPTION 'V5.2 reinforcement stage identifiers are invalid';
  END IF;
  packet := p_packet_text::jsonb;
  projection_value := packet->'projections'->0;
  identity := projection_value->'identity';
  payload := projection_value->'payload';
  input := projection_value->'observation_inputs'->0;
  target := projection_value->'target';
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|projection-v5-2-reinforce|'||p_plan_id::text,0
  ));
  SELECT stored.* INTO existing
  FROM memory.projection_plan AS stored
  WHERE stored.owner_user_id=actor
    AND (stored.plan_id=p_plan_id
         OR stored.packet_sha256=packet->>'packet_sha256');
  IF FOUND THEN
    IF existing.plan_id<>p_plan_id
       OR existing.packet_text<>p_packet_text
       OR existing.owner_manifest_sha256<>p_expected_owner_manifest_sha256
       OR NOT EXISTS (
         SELECT 1 FROM memory.projection_plan_item AS item
         WHERE item.owner_user_id=actor
           AND item.plan_id=p_plan_id
           AND item.projection_ref='p01'
           AND item.projection=projection_value
           AND item.target_action='reinforce'
           AND item.expected_revision_number
                =(target->>'expected_revision_number')::integer
       )
       OR NOT EXISTS (
         SELECT 1 FROM memory.projection_claim_payload AS claim_payload
         WHERE claim_payload.owner_user_id=actor
           AND claim_payload.plan_id=p_plan_id
           AND claim_payload.projection_ref='p01'
           AND claim_payload.target_action='reinforce'
           AND claim_payload.target_claim_id
                =(target->>'aggregate_id')::uuid
       )
       OR NOT EXISTS (
         SELECT 1 FROM memory.projection_plan_observation AS link
         WHERE link.owner_user_id=actor
           AND link.plan_id=p_plan_id
           AND link.projection_ref='p01'
           AND link.observation_id=(input->>'observation_id')::uuid
           AND link.observation_sha256=input->>'observation_sha256'
           AND link.stance='supports'
       ) THEN
      RAISE EXCEPTION 'V5.2 reinforcement replay state mismatch';
    END IF;
    RETURN QUERY SELECT p_plan_id,'replayed',0,jsonb_build_object(
      'plan_id',p_plan_id,'projection_ref','p01',
      'packet_sha256',existing.packet_sha256,
      'target_claim_id',target->>'aggregate_id',
      'expected_revision_number',
      (target->>'expected_revision_number')::integer
    );
    RETURN;
  END IF;
  SELECT * INTO STRICT preflight
  FROM memory.preflight_projection_reinforcement_v5_2(
    p_plan_id,p_packet_text
  );
  IF preflight.existing_aggregates<>1
     OR preflight.existing_plans<>0
     OR preflight.owner_manifest_sha256
          <>p_expected_owner_manifest_sha256 THEN
    RAISE EXCEPTION 'V5.2 reinforcement preflight is stale or mismatched';
  END IF;
  SELECT * INTO STRICT expected
  FROM memory.expected_projection_payload_v5_2(
    (input->>'observation_id')::uuid
  );
  INSERT INTO memory.projection_plan(
    owner_user_id,plan_id,contract_version,predicate_registry_version,
    projection_policy_version,projector,projector_version,packet_text,
    packet_text_sha256,packet_sha256,owner_manifest_sha256,
    projection_count,invoked_by_session
  ) VALUES (
    actor,p_plan_id,packet->>'contract_version',
    packet->>'predicate_registry_version',
    packet->>'projection_policy_version',packet->>'projector',
    packet->>'projector_version',p_packet_text,
    preflight.packet_text_sha256,preflight.packet_sha256,
    preflight.owner_manifest_sha256,1,session_user
  );
  INSERT INTO memory.projection_plan_item(
    owner_user_id,plan_id,projection_ref,predicate_registry_version,lane,
    projection,projection_sha256,subject_entity_id,predicate,object_kind,
    object_entity_id,object_literal_sha256,polarity,modality,lane_scope,
    semantic_key_sha256,target_action,expected_revision_number,
    target_reason_codes,temporal_materialization,
    temporal_source_observation_id,review_state,authorization_required,
    review_reason_codes
  ) VALUES (
    actor,p_plan_id,'p01','memory_predicate_registry_v5_2','claim',
    projection_value,preflight.projection_sha256,
    (identity->>'subject_entity_id')::uuid,
    identity->>'predicate',identity->>'object_kind',
    NULLIF(identity->>'object_entity_id','')::uuid,
    NULLIF(identity->>'object_literal_sha256',''),
    (identity->>'polarity')::memory.observation_polarity,
    (identity->>'modality')::memory.observation_modality,
    expected.lane_scope,preflight.semantic_key_sha256,'reinforce',
    preflight.target_revision_number,
    target->'reason_codes',
    expected.temporal_materialization,
    expected.temporal_source_observation_id,
    'manual_review_required',true,
    projection_value#>'{review,reason_codes}'
  );
  INSERT INTO memory.projection_claim_payload(
    owner_user_id,plan_id,projection_ref,target_action,target_claim_id,
    claim_class,canonical_text,surface_policy
  ) VALUES (
    actor,p_plan_id,'p01','reinforce',preflight.target_claim_id,
    payload->>'claim_class',payload->>'canonical_text',
    (payload->>'surface_policy')::memory.observation_surface_policy
  );
  INSERT INTO memory.projection_plan_observation(
    owner_user_id,plan_id,projection_ref,observation_id,
    observation_sha256,stance
  ) VALUES (
    actor,p_plan_id,'p01',(input->>'observation_id')::uuid,
    input->>'observation_sha256','supports'
  );
  SET CONSTRAINTS ALL IMMEDIATE;
  result_value := jsonb_build_object(
    'plan_id',p_plan_id,'projection_ref','p01',
    'packet_sha256',preflight.packet_sha256,
    'lane','claim','target_action','reinforce',
    'target_claim_id',preflight.target_claim_id,
    'expected_revision_number',preflight.target_revision_number
  );
  RETURN QUERY SELECT p_plan_id,'applied',4,result_value;
END
$function$;

ALTER FUNCTION memory.preflight_projection_reinforcement_v5_2(uuid,text)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.stage_projection_reinforcement_v5_2(uuid,text,text)
  OWNER TO memory_v5_writer;
ALTER FUNCTION
  memory.v5_2_canonical_name_reinforcement_policy_bridge(uuid,uuid,text,uuid)
  OWNER TO memory_v5_writer;

REVOKE ALL ON FUNCTION
  memory.preflight_projection_reinforcement_v5_2(uuid,text)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION
  memory.stage_projection_reinforcement_v5_2(uuid,text,text)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION
  memory.v5_2_canonical_name_reinforcement_policy_bridge(uuid,uuid,text,uuid)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.preflight_projection_reinforcement_v5_2(uuid,text)
  TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.stage_projection_reinforcement_v5_2(uuid,text,text)
  TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.v5_2_canonical_name_reinforcement_policy_bridge(uuid,uuid,text,uuid)
  TO memory_v5_writer;

COMMIT;
