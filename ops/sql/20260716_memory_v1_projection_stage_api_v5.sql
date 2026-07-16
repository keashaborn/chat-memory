BEGIN;

DO $prerequisite$
BEGIN
  IF to_regprocedure(
       'memory.preflight_projection_packet_v5(uuid,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'projection packet preflight API is missing';
  END IF;
END
$prerequisite$;

CREATE OR REPLACE FUNCTION memory.stage_projection_plan_v5(
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
SECURITY DEFINER
SET search_path = ''
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
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_plan_id IS NULL
     OR NOT memory.v5_sha256_valid(p_expected_owner_manifest_sha256) THEN
    RAISE EXCEPTION 'projection stage identifiers or manifest are invalid'
      USING ERRCODE = '22023';
  END IF;
  packet := p_packet_text::jsonb;
  projection_value := packet->'projections'->0;
  identity_value := projection_value->'identity';
  payload_value := projection_value->'payload';
  observation_input_value := projection_value->'observation_inputs'->0;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|projection-plan|'||p_plan_id::text,0
  ));
  SELECT stored.* INTO existing
  FROM memory.projection_plan AS stored
  WHERE stored.owner_user_id=actor
    AND (
      stored.plan_id=p_plan_id
      OR stored.packet_sha256=packet->>'packet_sha256'
    );
  IF FOUND THEN
    IF existing.plan_id <> p_plan_id
       OR existing.packet_text <> p_packet_text
       OR existing.owner_manifest_sha256
            <> p_expected_owner_manifest_sha256
       OR (SELECT count(*) FROM memory.projection_plan_item AS item
           WHERE item.owner_user_id=actor AND item.plan_id=p_plan_id) <> 1
       OR NOT EXISTS (
         SELECT 1 FROM memory.projection_plan_item AS item
         WHERE item.owner_user_id=actor AND item.plan_id=p_plan_id
           AND item.projection_ref='p01'
           AND item.projection=projection_value
       )
       OR NOT EXISTS (
         SELECT 1 FROM memory.projection_claim_payload AS claim_payload
         WHERE claim_payload.owner_user_id=actor
           AND claim_payload.plan_id=p_plan_id
           AND claim_payload.projection_ref='p01'
           AND claim_payload.canonical_text
                =payload_value->>'canonical_text'
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
      RAISE EXCEPTION 'projection stage replay state mismatch'
        USING ERRCODE = '23514';
    END IF;
    result_value := jsonb_build_object(
      'plan_id',p_plan_id,'projection_ref','p01',
      'packet_sha256',existing.packet_sha256
    );
    RETURN QUERY SELECT p_plan_id,'replayed',0,result_value;
    RETURN;
  END IF;
  SELECT * INTO preflight
  FROM memory.preflight_projection_packet_v5(p_plan_id,p_packet_text);
  IF preflight.existing_claims <> 0 OR preflight.existing_plans <> 0
     OR preflight.owner_manifest_sha256
          <> p_expected_owner_manifest_sha256 THEN
    RAISE EXCEPTION 'projection stage preflight is stale or mismatched'
      USING ERRCODE = '23514';
  END IF;
  INSERT INTO memory.projection_plan(
    owner_user_id,plan_id,contract_version,predicate_registry_version,
    projection_policy_version,projector,projector_version,
    packet_text,packet_text_sha256,packet_sha256,
    owner_manifest_sha256,projection_count,invoked_by_session
  ) VALUES (
    actor,p_plan_id,packet->>'contract_version',
    packet->>'predicate_registry_version',
    packet->>'projection_policy_version',
    packet->>'projector',packet->>'projector_version',
    p_packet_text,preflight.packet_text_sha256,
    preflight.packet_sha256,preflight.owner_manifest_sha256,
    1,session_user
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
    identity_value->>'predicate',
    identity_value->>'object_kind',
    (identity_value->>'object_entity_id')::uuid,NULL,
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
    payload_value->>'claim_class',
    payload_value->>'canonical_text',
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
  result_value := jsonb_build_object(
    'plan_id',p_plan_id,'projection_ref','p01',
    'packet_sha256',preflight.packet_sha256
  );
  RETURN QUERY SELECT p_plan_id,'applied',4,result_value;
END
$function$;

ALTER FUNCTION memory.stage_projection_plan_v5(uuid,text,text)
  OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION memory.stage_projection_plan_v5(uuid,text,text)
  FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION memory.stage_projection_plan_v5(uuid,text,text)
  TO brains_app;

COMMIT;
