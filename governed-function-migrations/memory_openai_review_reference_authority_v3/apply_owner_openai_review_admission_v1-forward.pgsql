CREATE OR REPLACE FUNCTION memory.apply_owner_openai_review_admission_v1(p_entity_resolution_request_id uuid, p_route_event_id uuid, p_expected_stage_bundle_sha256 text, p_reviewer_type text, p_reviewer_ref text, p_reason text, p_reason_codes jsonb, p_admission_manifest_sha256 text)
RETURNS TABLE(admission_id uuid, operation_id uuid, route_event_id uuid, packet_id uuid, evidence_id uuid, batch_id uuid, observation_id uuid, outcome text, rows_written integer, result jsonb)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  authority record;
  source memory.v5_2_openai_packet_route_event%ROWTYPE;
  extraction jsonb;
  observation jsonb;
  observation_ref text;
  subject_entity_ref text;
  staged record;
  resolution_authority record;
  resolution_applied record;
  new_resolution_id uuid;
  new_observation_id uuid;
  result_value jsonb;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'OpenAI review admission requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required' USING ERRCODE='42501';
  END IF;
  IF p_entity_resolution_request_id IS NULL
     OR p_route_event_id IS NULL
     OR p_entity_resolution_request_id=p_route_event_id
     OR p_admission_manifest_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'OpenAI review admission identifiers are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_openai_review_admission',actor::text,
      p_route_event_id::text),0
  ));
  SELECT value.* INTO authority
  FROM memory.preflight_owner_openai_review_admission_v1(
    p_route_event_id,p_expected_stage_bundle_sha256,p_reviewer_type,
    p_reviewer_ref,p_reason,p_reason_codes
  ) AS value;
  IF authority.admission_manifest_sha256<>p_admission_manifest_sha256 THEN
    RAISE EXCEPTION 'OpenAI review admission manifest mismatch'
      USING ERRCODE='23514';
  END IF;
  SELECT value.* INTO STRICT source
  FROM memory.v5_2_openai_packet_route_event AS value
  WHERE value.owner_user_id=actor AND value.route_event_id=p_route_event_id;
  extraction:=(source.stage_bundle->>'extraction_packet_text')::jsonb;
  SELECT value INTO observation
  FROM jsonb_array_elements(extraction->'observations') AS value;
  observation_ref:=observation->>'observation_ref';
  subject_entity_ref:=observation->>'subject_entity_ref';
  IF observation_ref<>authority.observation_ref
     OR observation_ref !~ '^o[0-9]{2}$'
     OR subject_entity_ref !~ '^e[0-9]{2}$' THEN
    RAISE EXCEPTION 'OpenAI review admission packet references conflict'
      USING ERRCODE='23514';
  END IF;
  SELECT value.* INTO staged
  FROM memory.stage_relational_packet_v5_2(
    authority.stage_request_id,source.evidence_id,
    source.stage_bundle->>'extractor',source.stage_bundle->>'extractor_version',
    source.stage_bundle->>'extraction_packet_text',
    source.stage_bundle->>'resolution_packet_text',
    source.stage_bundle->>'extraction_packet_sha256',
    source.stage_bundle->>'resolution_packet_sha256'
  ) AS value;
  new_observation_id:=(staged.result #>> ARRAY['observation_ids',observation_ref])::uuid;
  new_resolution_id:=(staged.result #>> ARRAY['resolution_ids',subject_entity_ref])::uuid;
  IF staged.outcome NOT IN ('applied','replayed')
     OR staged.observations_inserted NOT IN (0,1)
     OR new_observation_id IS NULL OR new_resolution_id IS NULL THEN
    RAISE EXCEPTION 'OpenAI review relational stage outcome is invalid'
      USING ERRCODE='23514';
  END IF;
  SELECT value.* INTO resolution_authority
  FROM memory.preflight_entity_resolution_apply_v5(new_resolution_id,NULL) AS value;
  IF resolution_authority.action::text<>'link_existing'
     OR resolution_authority.decision_state::text<>'auto_link_eligible'
     OR resolution_authority.review_id IS NOT NULL
     OR resolution_authority.prospective_entity_id IS NULL THEN
    RAISE EXCEPTION 'OpenAI review entity resolution is not an exact existing link'
      USING ERRCODE='23514';
  END IF;
  SELECT value.* INTO resolution_applied
  FROM memory.apply_entity_resolution_v5(
    p_entity_resolution_request_id,new_resolution_id,NULL,
    resolution_authority.apply_manifest_sha256
  ) AS value;
  IF resolution_applied.outcome NOT IN ('applied','replayed')
     OR resolution_applied.applied_entity_id
          <>resolution_authority.prospective_entity_id
     OR resolution_applied.bindings_created NOT IN (0,1) THEN
    RAISE EXCEPTION 'OpenAI review entity resolution apply outcome is invalid'
      USING ERRCODE='23514';
  END IF;
  result_value:=jsonb_build_object(
    'admission_id',source.route_event_id,
    'operation_id',authority.stage_request_id,
    'route_event_id',source.route_event_id,
    'packet_id',source.packet_id,
    'evidence_id',source.evidence_id,
    'batch_id',staged.batch_id,
    'observation_id',new_observation_id,
    'observation_ref',observation_ref,
    'subject_entity_ref',subject_entity_ref,
    'entity_resolution_request_id',p_entity_resolution_request_id,
    'resolution_id',new_resolution_id,
    'selected_entity_id',resolution_authority.prospective_entity_id,
    'admission_manifest_sha256',p_admission_manifest_sha256
  );
  RETURN QUERY SELECT source.route_event_id,authority.stage_request_id,
    source.route_event_id,source.packet_id,source.evidence_id,staged.batch_id,
    new_observation_id,
    CASE WHEN staged.outcome='replayed' AND resolution_applied.outcome='replayed'
      THEN 'replayed'::text ELSE 'applied'::text END,
    staged.mentions_inserted+staged.resolutions_inserted+
      staged.candidates_inserted+staged.observations_inserted+
      staged.temporals_inserted+resolution_applied.bindings_created,
    result_value;
END
$function$;

ALTER FUNCTION memory.apply_owner_openai_review_admission_v1(uuid,uuid,text,text,text,text,jsonb,text) OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION memory.apply_owner_openai_review_admission_v1(uuid,uuid,text,text,text,text,jsonb,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.apply_owner_openai_review_admission_v1(uuid,uuid,text,text,text,text,jsonb,text) FROM brains_app;
REVOKE ALL ON FUNCTION memory.apply_owner_openai_review_admission_v1(uuid,uuid,text,text,text,text,jsonb,text) FROM memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.apply_owner_openai_review_admission_v1(uuid,uuid,text,text,text,text,jsonb,text) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.apply_owner_openai_review_admission_v1(uuid,uuid,text,text,text,text,jsonb,text) TO memory_v5_writer;
