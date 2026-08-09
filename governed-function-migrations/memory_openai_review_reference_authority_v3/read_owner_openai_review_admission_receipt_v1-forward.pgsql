CREATE OR REPLACE FUNCTION memory.read_owner_openai_review_admission_receipt_v1(p_route_event_id uuid, p_expected_stage_bundle_sha256 text, p_entity_resolution_request_id uuid, p_plan_id uuid, p_entailment_request_id uuid, p_reviewer_type text, p_reviewer_ref text, p_reason text, p_reason_codes jsonb, p_expected_admission_manifest_sha256 text)
RETURNS TABLE(owner_user_id_sha256 text, command_manifest_sha256 text, admission_manifest_sha256 text, admission_id uuid, operation_id uuid, entailment_request_id uuid, route_event_id uuid, packet_id uuid, evidence_id uuid, batch_id uuid, observation_id uuid, plan_id uuid, projection_review_id uuid, projection_review_authorization_sha256 text, projection_apply_manifest_sha256 text, receipt_sha256 text, outcome text, rows_written integer)
LANGUAGE plpgsql
STABLE
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
  stage_request memory.relational_operation_request%ROWTYPE;
  resolution_request memory.relational_operation_request%ROWTYPE;
  entailment_request memory.relational_operation_request%ROWTYPE;
  plan memory.projection_plan%ROWTYPE;
  item memory.projection_plan_item%ROWTYPE;
  review memory.projection_review%ROWTYPE;
  normalized_reason_codes jsonb;
  new_resolution_id uuid;
  new_observation_id uuid;
  batch_id_value uuid;
  owner_sha text;
  command_sha text;
  apply_sha text;
  receipt_sha text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'OpenAI review admission receipt read requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required' USING ERRCODE='42501';
  END IF;
  IF p_entity_resolution_request_id IS NULL OR p_plan_id IS NULL
     OR p_entailment_request_id IS NULL
     OR p_expected_admission_manifest_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'OpenAI review admission receipt identity is invalid'
      USING ERRCODE='22023';
  END IF;
  SELECT value.* INTO authority
  FROM memory.preflight_owner_openai_review_admission_v1(
    p_route_event_id,p_expected_stage_bundle_sha256,p_reviewer_type,
    p_reviewer_ref,p_reason,p_reason_codes
  ) AS value;
  IF authority.admission_manifest_sha256<>p_expected_admission_manifest_sha256 THEN
    RAISE EXCEPTION 'OpenAI review admission receipt manifest conflicts'
      USING ERRCODE='23514';
  END IF;
  SELECT jsonb_agg(value ORDER BY value#>>'{}') INTO normalized_reason_codes
  FROM jsonb_array_elements(p_reason_codes) AS value;
  SELECT value.* INTO source
  FROM memory.v5_2_openai_packet_route_event AS value
  WHERE value.owner_user_id=actor
    AND value.route_event_id=p_route_event_id;
  extraction:=(source.stage_bundle->>'extraction_packet_text')::jsonb;
  SELECT value INTO observation
  FROM jsonb_array_elements(extraction->'observations') AS value;
  observation_ref:=observation->>'observation_ref';
  subject_entity_ref:=observation->>'subject_entity_ref';
  IF observation_ref<>authority.observation_ref
     OR observation_ref !~ '^o[0-9]{2}$'
     OR subject_entity_ref !~ '^e[0-9]{2}$' THEN
    RAISE EXCEPTION 'OpenAI review admission receipt packet references conflict'
      USING ERRCODE='23514';
  END IF;
  SELECT value.* INTO stage_request
  FROM memory.relational_operation_request AS value
  WHERE value.owner_user_id=actor
    AND value.request_id=authority.stage_request_id;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  IF stage_request.operation<>'stage_packet'
     OR stage_request.target_key<>authority.evidence_id::text
     OR stage_request.outcome<>'applied'
     OR (stage_request.result->>'batch_id') IS NULL
     OR (stage_request.result #>> ARRAY['observation_ids',observation_ref]) IS NULL
     OR (stage_request.result #>> ARRAY['resolution_ids',subject_entity_ref]) IS NULL THEN
    RAISE EXCEPTION 'OpenAI review admission stage receipt conflicts'
      USING ERRCODE='23514';
  END IF;
  batch_id_value:=(stage_request.result->>'batch_id')::uuid;
  new_observation_id:=(stage_request.result #>> ARRAY['observation_ids',observation_ref])::uuid;
  new_resolution_id:=(stage_request.result #>> ARRAY['resolution_ids',subject_entity_ref])::uuid;
  SELECT value.* INTO resolution_request
  FROM memory.relational_operation_request AS value
  WHERE value.owner_user_id=actor
    AND value.request_id=p_entity_resolution_request_id;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  IF resolution_request.operation<>'apply_resolution'
     OR resolution_request.target_key<>new_resolution_id::text
     OR resolution_request.outcome<>'applied'
     OR resolution_request.result->>'resolution_id'<>new_resolution_id::text
     OR resolution_request.result->>'applied_entity_id' IS NULL
     OR NOT EXISTS (
       SELECT 1 FROM memory.observation_entity_binding AS binding
       WHERE binding.owner_user_id=actor
         AND binding.observation_id=new_observation_id
         AND binding.subject_resolution_id=new_resolution_id
         AND binding.subject_entity_id=(
           resolution_request.result->>'applied_entity_id'
         )::uuid
     ) THEN
    RAISE EXCEPTION 'OpenAI review admission resolution receipt conflicts'
      USING ERRCODE='23514';
  END IF;
  SELECT value.* INTO entailment_request
  FROM memory.relational_operation_request AS value
  WHERE value.owner_user_id=actor
    AND value.request_id=p_entailment_request_id;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  IF entailment_request.operation<>'record_observation_entailment_v5'
     OR entailment_request.target_key<>new_observation_id::text
     OR entailment_request.outcome<>'applied'
     OR entailment_request.result->>'observation_id'<>new_observation_id::text
     OR entailment_request.result->>'decision'<>'accepted'
     OR NOT EXISTS (
       SELECT 1 FROM memory.observation_entailment_v5 AS entailment
       WHERE entailment.owner_user_id=actor
         AND entailment.decision_id=(
           entailment_request.result->>'decision_id'
         )::uuid
         AND entailment.observation_id=new_observation_id
         AND entailment.decision='accepted'
         AND entailment.reason_code='predicate_entailment_v5_1_accepted'
         AND entailment.assessor_type='system'
         AND entailment.assessor_ref='memory_v1_openai_review_admission_v1'
     ) THEN
    RAISE EXCEPTION 'OpenAI review admission entailment receipt conflicts'
      USING ERRCODE='23514';
  END IF;
  SELECT value.* INTO plan
  FROM memory.projection_plan AS value
  WHERE value.owner_user_id=actor AND value.plan_id=p_plan_id;
  SELECT value.* INTO item
  FROM memory.projection_plan_item AS value
  WHERE value.owner_user_id=actor
    AND value.plan_id=p_plan_id
    AND value.projection_ref='p01';
  IF plan.plan_id IS NULL OR item.plan_id IS NULL THEN
    RETURN;
  END IF;
  IF plan.contract_version<>'memory_v1_projection_plan_v5'
     OR plan.predicate_registry_version<>'memory_predicate_registry_v5_2'
     OR plan.projection_policy_version<>'memory_projection_policy_v5'
     OR plan.projection_count<>1
     OR plan.packet_text_sha256<>memory.v5_digest_text(plan.packet_text)
     OR plan.packet_sha256<>memory.v5_digest_text(
       memory.v5_canonical_json_text((plan.packet_text::jsonb)-'packet_sha256')
     )
     OR plan.owner_manifest_sha256<>
       memory.v5_projection_owner_manifest_sha256(actor,plan.packet_sha256)
     OR item.lane<>'claim' OR item.target_action<>'create'
     OR item.expected_revision_number IS NOT NULL
     OR item.review_state<>'manual_review_required'
     OR NOT item.authorization_required
     OR item.projection_sha256<>memory.v5_digest_text(
       memory.v5_canonical_json_text(item.projection)
     )
     OR item.semantic_key_sha256<>
       memory.v5_projection_semantic_key_sha256(
         item.owner_user_id,item.lane,item.subject_entity_id,item.predicate,
         item.object_kind,item.object_entity_id,item.object_literal_sha256,
         item.polarity,item.modality,item.lane_scope
       )
     OR NOT EXISTS (
       SELECT 1 FROM memory.projection_plan_observation AS link
       WHERE link.owner_user_id=actor
         AND link.plan_id=p_plan_id
         AND link.projection_ref='p01'
         AND link.observation_id=new_observation_id
     ) THEN
    RAISE EXCEPTION 'OpenAI review admission projection plan conflicts'
      USING ERRCODE='23514';
  END IF;
  SELECT value.* INTO review
  FROM memory.projection_review AS value
  WHERE value.owner_user_id=actor
    AND value.plan_id=p_plan_id
    AND value.projection_ref='p01'
    AND value.decision='authorized'
    AND value.reviewer_type=p_reviewer_type
    AND value.reviewer_ref IS NOT DISTINCT FROM btrim(p_reviewer_ref)
    AND value.reason=btrim(p_reason)
    AND value.reason_codes=normalized_reason_codes
  ORDER BY value.review_number DESC
  LIMIT 1;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  IF review.parent_review_state<>'manual_review_required'
     OR review.expected_projection_sha256<>item.projection_sha256
     OR review.expected_semantic_key_sha256<>item.semantic_key_sha256
     OR review.authorization_manifest_sha256<>
       memory.v5_projection_review_manifest_sha256(
         review.owner_user_id,review.plan_id,review.projection_ref,
         review.expected_projection_sha256,review.expected_semantic_key_sha256,
         review.review_number,review.decision,review.reviewer_type,
         review.reviewer_ref,review.reason,review.reason_codes
       )
     OR EXISTS (
       SELECT 1 FROM memory.projection_review AS later
       WHERE later.owner_user_id=actor
         AND later.plan_id=p_plan_id
         AND later.projection_ref='p01'
         AND later.review_number>review.review_number
     ) THEN
    RAISE EXCEPTION 'OpenAI review admission projection review conflicts'
      USING ERRCODE='23514';
  END IF;
  apply_sha:=memory.v5_projection_apply_manifest_sha256(
    actor,plan.owner_manifest_sha256,p_plan_id,'p01',item.projection_sha256,
    item.semantic_key_sha256,item.lane,item.target_action,0,review.review_id,
    review.authorization_manifest_sha256
  );
  owner_sha:=encode(public.digest(convert_to(actor::text,'UTF8'),'sha256'),'hex');
  command_sha:=encode(public.digest(convert_to(jsonb_build_object(
    'contract_version','memory_v1_openai_review_admission_command_v1',
    'entity_resolution_request_id',p_entity_resolution_request_id::text,
    'route_event_id',p_route_event_id::text,
    'expected_stage_bundle_sha256',p_expected_stage_bundle_sha256,
    'plan_id',p_plan_id::text,
    'entailment_request_id',p_entailment_request_id::text,
    'reason',btrim(p_reason),
    'reason_codes',normalized_reason_codes,
    'policy_version','memory_v1_openai_review_admission_policy_v1'
  )::text,'UTF8'),'sha256'),'hex');
  receipt_sha:=encode(public.digest(convert_to(concat_ws('|',
    'memory_v1_openai_review_admission_receipt_v1',owner_sha,command_sha,
    authority.admission_manifest_sha256,p_route_event_id::text,
    authority.stage_request_id::text,p_entailment_request_id::text,
    p_route_event_id::text,source.packet_id::text,source.evidence_id::text,
    batch_id_value::text,new_observation_id::text,p_plan_id::text,
    review.review_id::text,review.authorization_manifest_sha256,apply_sha
  ),'UTF8'),'sha256'),'hex');
  RETURN QUERY SELECT owner_sha,command_sha,authority.admission_manifest_sha256,
    p_route_event_id,authority.stage_request_id,p_entailment_request_id,
    p_route_event_id,source.packet_id,source.evidence_id,batch_id_value,
    new_observation_id,p_plan_id,review.review_id,
    review.authorization_manifest_sha256,apply_sha,receipt_sha,
    'replayed'::text,0;
END
$function$;

ALTER FUNCTION memory.read_owner_openai_review_admission_receipt_v1(uuid,text,uuid,uuid,uuid,text,text,text,jsonb,text) OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION memory.read_owner_openai_review_admission_receipt_v1(uuid,text,uuid,uuid,uuid,text,text,text,jsonb,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.read_owner_openai_review_admission_receipt_v1(uuid,text,uuid,uuid,uuid,text,text,text,jsonb,text) FROM brains_app;
REVOKE ALL ON FUNCTION memory.read_owner_openai_review_admission_receipt_v1(uuid,text,uuid,uuid,uuid,text,text,text,jsonb,text) FROM memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.read_owner_openai_review_admission_receipt_v1(uuid,text,uuid,uuid,uuid,text,text,text,jsonb,text) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.read_owner_openai_review_admission_receipt_v1(uuid,text,uuid,uuid,uuid,text,text,text,jsonb,text) TO memory_v5_writer;
