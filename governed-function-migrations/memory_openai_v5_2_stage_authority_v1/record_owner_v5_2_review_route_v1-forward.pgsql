CREATE OR REPLACE FUNCTION memory.record_owner_v5_2_review_route_v1(p_operation_id uuid, p_route_event_id uuid, p_packet_id uuid, p_expected_packet_storage_sha256 text, p_expected_routing_basis_sha256 text, p_review_id uuid, p_request_id uuid, p_review_report_sha256 text, p_stage_bundle_sha256 text, p_repository_commit text, p_auto_link_count integer, p_manual_review_count integer, p_deferred_resolution_count integer, p_rejected_count integer, p_blocking_code_count integer)
RETURNS TABLE(route_event_id uuid, packet_id uuid, route text, apply_outcome text)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  target record;
  packet record;
  replayed record;
  replay_count integer;
  packet_count integer;
  review_report_text text := nullif(
    current_setting('memory.openai_review_report_v1',true),''
  );
  stage_bundle_text text := nullif(
    current_setting('memory.openai_stage_bundle_v1',true),''
  );
  review_report_value jsonb;
  stage_bundle_value jsonb;
  calculated_review_sha text;
  calculated_bundle_sha text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5.2 review route requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_route_event_id IS NULL OR p_packet_id IS NULL
     OR p_review_id IS NULL OR p_request_id IS NULL
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_routing_basis_sha256 !~ '^[0-9a-f]{64}$'
     OR p_review_report_sha256 !~ '^[0-9a-f]{64}$'
     OR p_stage_bundle_sha256 !~ '^[0-9a-f]{64}$'
     OR p_repository_commit !~ '^[0-9a-f]{40}$'
     OR p_auto_link_count NOT BETWEEN 0 AND 32
     OR p_manual_review_count NOT BETWEEN 0 AND 32
     OR p_deferred_resolution_count NOT BETWEEN 0 AND 32
     OR p_rejected_count NOT BETWEEN 0 AND 32
     OR p_blocking_code_count NOT BETWEEN 0 AND 32 THEN
    RAISE EXCEPTION 'V5.2 review route inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  IF (review_report_text IS NULL)<>(stage_bundle_text IS NULL) THEN
    RAISE EXCEPTION 'OpenAI review artifact pair is incomplete'
      USING ERRCODE='22023';
  END IF;
  IF review_report_text IS NOT NULL THEN
    review_report_value:=review_report_text::jsonb;
    stage_bundle_value:=stage_bundle_text::jsonb;
    calculated_review_sha:=encode(public.digest(
      convert_to(review_report_value::text,'UTF8'),'sha256'
    ),'hex');
    calculated_bundle_sha:=encode(public.digest(
      convert_to(stage_bundle_value::text,'UTF8'),'sha256'
    ),'hex');
    IF jsonb_typeof(review_report_value)<>'object'
       OR jsonb_typeof(stage_bundle_value)<>'object'
       OR pg_column_size(review_report_value)>196608
       OR pg_column_size(stage_bundle_value)>262144
       OR calculated_review_sha<>p_review_report_sha256
       OR calculated_bundle_sha<>p_stage_bundle_sha256
       OR review_report_value->>'contract_version'
          <>'memory_v1_v5_2_openai_packet_review_v1'
       OR review_report_value->>'owner_user_id'<>actor::text
       OR review_report_value->>'packet_id'<>p_packet_id::text
       OR review_report_value->>'review_id'<>p_review_id::text
       OR review_report_value->>'repository_commit'<>p_repository_commit
       OR review_report_value->>'review_disposition'
          <>'manual_review_required'
       OR stage_bundle_value->>'contract_version'
          <>'memory_v1_v5_2_stage_preflight_v1'
       OR stage_bundle_value->>'owner_user_id'<>actor::text
       OR stage_bundle_value->>'request_id'<>p_request_id::text
       OR stage_bundle_value->>'case_id'
          <>concat('openai-packet-',p_packet_id::text)
       OR stage_bundle_value#>>'{source_report,storage}'
          <>'memory.v5_2_openai_packet_route_event.review_report'
       OR stage_bundle_value#>>'{source_report,sha256}'
          <>p_review_report_sha256
       OR (stage_bundle_value->>'database_writes')::integer<>0
       OR (stage_bundle_value->>'qdrant_writes')::integer<>0
       OR (stage_bundle_value->>'external_model_calls')::integer<>0
       OR (stage_bundle_value->>'authorized_stage')::boolean IS DISTINCT FROM false
       OR (review_report_value#>>'{resolution_summary,auto_link_eligible}')::integer
          <>p_auto_link_count
       OR (review_report_value#>>'{resolution_summary,manual_review_required}')::integer
          <>p_manual_review_count
       OR (review_report_value#>>'{resolution_summary,deferred}')::integer
          <>p_deferred_resolution_count
       OR (review_report_value#>>'{resolution_summary,rejected}')::integer
          <>p_rejected_count THEN
      RAISE EXCEPTION 'OpenAI review artifacts are invalid or unbound'
        USING ERRCODE='23514';
    END IF;
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_2_packet_route',actor::text,p_packet_id::text
  ),0));
  SELECT count(*) INTO replay_count
  FROM (
    SELECT local_event.operation_id,local_event.packet_id
    FROM memory.v5_2_local_packet_route_event AS local_event
    WHERE local_event.owner_user_id=actor
      AND (local_event.operation_id=p_operation_id
           OR local_event.packet_id=p_packet_id)
    UNION ALL
    SELECT openai_event.operation_id,openai_event.packet_id
    FROM memory.v5_2_openai_packet_route_event AS openai_event
    WHERE openai_event.owner_user_id=actor
      AND (openai_event.operation_id=p_operation_id
           OR openai_event.packet_id=p_packet_id)
  ) AS prior;
  IF replay_count>1 THEN
    RAISE EXCEPTION 'V5.2 review route authority is ambiguous'
      USING ERRCODE='23514';
  END IF;
  SELECT prior.* INTO replayed
  FROM (
    SELECT 'local'::text AS source_kind,
      local_event.route_event_id,local_event.operation_id,local_event.packet_id,
      local_event.route,local_event.reason_code,
      local_event.packet_storage_sha256,local_event.routing_basis_sha256,
      local_event.review_id,local_event.request_id,
      local_event.review_contract,local_event.bundle_contract,
      local_event.review_report_sha256,local_event.stage_bundle_sha256,
      NULL::jsonb AS review_report,NULL::jsonb AS stage_bundle,
      local_event.repository_commit,local_event.auto_link_count,
      local_event.manual_review_count,local_event.deferred_resolution_count,
      local_event.rejected_count,local_event.blocking_code_count
    FROM memory.v5_2_local_packet_route_event AS local_event
    WHERE local_event.owner_user_id=actor
      AND (local_event.operation_id=p_operation_id
           OR local_event.packet_id=p_packet_id)
    UNION ALL
    SELECT 'openai'::text AS source_kind,
      openai_event.route_event_id,openai_event.operation_id,openai_event.packet_id,
      openai_event.route,openai_event.reason_code,
      openai_event.packet_storage_sha256,openai_event.routing_basis_sha256,
      openai_event.review_id,openai_event.request_id,
      openai_event.review_contract,openai_event.bundle_contract,
      openai_event.review_report_sha256,openai_event.stage_bundle_sha256,
      openai_event.review_report,openai_event.stage_bundle,
      openai_event.repository_commit,openai_event.auto_link_count,
      openai_event.manual_review_count,openai_event.deferred_resolution_count,
      openai_event.rejected_count,openai_event.blocking_code_count
    FROM memory.v5_2_openai_packet_route_event AS openai_event
    WHERE openai_event.owner_user_id=actor
      AND (openai_event.operation_id=p_operation_id
           OR openai_event.packet_id=p_packet_id)
  ) AS prior;
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.route_event_id<>p_route_event_id
       OR replayed.packet_id<>p_packet_id
       OR replayed.route<>'manual_review_artifact_ready'
       OR replayed.reason_code<>'reviewable_relational_packet_v5_2'
       OR replayed.bundle_contract<>'memory_v1_v5_2_stage_preflight_v1'
       OR (replayed.source_kind='local' AND
           replayed.review_contract<>'memory_v1_v5_2_local_packet_review_v1')
       OR (replayed.source_kind='openai' AND
           replayed.review_contract<>'memory_v1_v5_2_openai_packet_review_v1')
       OR replayed.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR replayed.routing_basis_sha256<>p_expected_routing_basis_sha256
       OR replayed.review_id<>p_review_id OR replayed.request_id<>p_request_id
       OR replayed.review_report_sha256<>p_review_report_sha256
       OR replayed.stage_bundle_sha256<>p_stage_bundle_sha256
       OR (replayed.source_kind='openai' AND (
         review_report_value IS NULL
         OR replayed.review_report<>review_report_value
         OR replayed.stage_bundle<>stage_bundle_value
       ))
       OR replayed.repository_commit<>p_repository_commit
       OR replayed.auto_link_count<>p_auto_link_count
       OR replayed.manual_review_count<>p_manual_review_count
       OR replayed.deferred_resolution_count<>p_deferred_resolution_count
       OR replayed.rejected_count<>p_rejected_count
       OR replayed.blocking_code_count<>p_blocking_code_count THEN
      RAISE EXCEPTION 'V5.2 review route replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.route_event_id,replayed.packet_id,
      replayed.route,'replayed'::text;
    RETURN;
  END IF;
  SELECT planned.* INTO target
  FROM memory.plan_owner_v5_2_exact_packet_route_v1(p_packet_id) AS planned
  WHERE planned.packet_id=p_packet_id
    AND planned.route='manual_review_artifact_ready';
  IF NOT FOUND
     OR target.packet_storage_sha256<>p_expected_packet_storage_sha256
     OR target.routing_basis_sha256<>p_expected_routing_basis_sha256 THEN
    RAISE EXCEPTION 'packet is not eligible for V5.2 review routing'
      USING ERRCODE='23514';
  END IF;
  SELECT count(*) INTO packet_count
  FROM (
    SELECT local_packet.packet_id
    FROM memory.evidence_extraction_packet_v5_local AS local_packet
    WHERE local_packet.owner_user_id=actor
      AND local_packet.packet_id=p_packet_id
    UNION ALL
    SELECT openai_packet.packet_id
    FROM memory.evidence_extraction_packet_v5 AS openai_packet
    WHERE openai_packet.owner_user_id=actor
      AND openai_packet.packet_id=p_packet_id
  ) AS source;
  IF packet_count<>1 THEN
    RAISE EXCEPTION 'V5.2 review packet authority is absent or ambiguous'
      USING ERRCODE='23514';
  END IF;
  SELECT source.* INTO STRICT packet
  FROM (
    SELECT 'local'::text AS source_kind,local_packet.packet_id,
      local_packet.job_id,local_packet.evidence_id,
      local_packet.evidence_content_sha256,local_packet.validator_packet_sha256,
      local_packet.packet_storage_sha256,local_packet.entity_mention_count,
      local_packet.observation_count,local_packet.comparison_hint_count,
      local_packet.deferral_count,local_packet.provider_id,
      NULL::text AS provider_version,local_packet.external_model_calls,
      local_packet.normalized_packet
    FROM memory.evidence_extraction_packet_v5_local AS local_packet
    WHERE local_packet.owner_user_id=actor
      AND local_packet.packet_id=p_packet_id
    UNION ALL
    SELECT 'openai'::text,openai_packet.packet_id,openai_packet.job_id,
      openai_packet.evidence_id,openai_packet.evidence_content_sha256,
      openai_packet.validator_packet_sha256,
      openai_packet.packet_storage_sha256,openai_packet.entity_mention_count,
      openai_packet.observation_count,openai_packet.comparison_hint_count,
      openai_packet.deferral_count,openai_packet.provider_id,
      openai_packet.provider_version,openai_packet.external_model_calls,
      openai_packet.normalized_packet
    FROM memory.evidence_extraction_packet_v5 AS openai_packet
    WHERE openai_packet.owner_user_id=actor
      AND openai_packet.packet_id=p_packet_id
  ) AS source;
  IF p_auto_link_count+p_manual_review_count+p_deferred_resolution_count
       +p_rejected_count<>packet.entity_mention_count THEN
    RAISE EXCEPTION 'V5.2 review resolution counts do not match packet'
      USING ERRCODE='23514';
  END IF;
  IF (packet.source_kind='openai' AND review_report_value IS NULL)
     OR (packet.source_kind='local' AND review_report_value IS NOT NULL) THEN
    RAISE EXCEPTION 'review artifact authority does not match packet source'
      USING ERRCODE='23514';
  END IF;
  IF packet.source_kind='openai' AND (
       packet.provider_id<>'openai_responses'
       OR packet.provider_version<>'v1'
       OR packet.external_model_calls<>1
       OR packet.normalized_packet->>'contract_version'
            <>'memory_v1_relational_extraction_v5_2'
       OR packet.normalized_packet->>'predicate_registry_version'
            <>'memory_predicate_registry_v5_2'
     ) THEN
    RAISE EXCEPTION 'OpenAI V5.2 review packet provenance is invalid'
      USING ERRCODE='23514';
  END IF;
  IF packet.source_kind='local' THEN
    INSERT INTO memory.v5_2_local_packet_route_event(
      route_event_id,owner_user_id,operation_id,packet_id,job_id,evidence_id,
      route,reason_code,routing_basis_sha256,evidence_content_sha256,
      validator_packet_sha256,packet_storage_sha256,entity_mention_count,
      observation_count,comparison_hint_count,deferral_count,
      source_deferral_reason_codes,review_id,request_id,review_contract,
      bundle_contract,review_report_sha256,stage_bundle_sha256,
      repository_commit,auto_link_count,manual_review_count,
      deferred_resolution_count,rejected_count,blocking_code_count
    ) VALUES (
      p_route_event_id,actor,p_operation_id,packet.packet_id,packet.job_id,
      packet.evidence_id,'manual_review_artifact_ready',
      'reviewable_relational_packet_v5_2',p_expected_routing_basis_sha256,
      packet.evidence_content_sha256,packet.validator_packet_sha256,
      packet.packet_storage_sha256,packet.entity_mention_count,
      packet.observation_count,packet.comparison_hint_count,
      packet.deferral_count,ARRAY[]::text[],p_review_id,p_request_id,
      'memory_v1_v5_2_local_packet_review_v1',
      'memory_v1_v5_2_stage_preflight_v1',p_review_report_sha256,
      p_stage_bundle_sha256,p_repository_commit,p_auto_link_count,
      p_manual_review_count,p_deferred_resolution_count,p_rejected_count,
      p_blocking_code_count
    );
  ELSE
    INSERT INTO memory.v5_2_openai_packet_route_event(
      route_event_id,owner_user_id,operation_id,packet_id,job_id,evidence_id,
      route,reason_code,routing_basis_sha256,evidence_content_sha256,
      validator_packet_sha256,packet_storage_sha256,entity_mention_count,
      observation_count,comparison_hint_count,deferral_count,
      source_deferral_reason_codes,review_id,request_id,review_contract,
      bundle_contract,review_report_sha256,stage_bundle_sha256,
      review_report,stage_bundle,
      repository_commit,auto_link_count,manual_review_count,
      deferred_resolution_count,rejected_count,blocking_code_count
    ) VALUES (
      p_route_event_id,actor,p_operation_id,packet.packet_id,packet.job_id,
      packet.evidence_id,'manual_review_artifact_ready',
      'reviewable_relational_packet_v5_2',p_expected_routing_basis_sha256,
      packet.evidence_content_sha256,packet.validator_packet_sha256,
      packet.packet_storage_sha256,packet.entity_mention_count,
      packet.observation_count,packet.comparison_hint_count,
      packet.deferral_count,ARRAY[]::text[],p_review_id,p_request_id,
      'memory_v1_v5_2_openai_packet_review_v1',
      'memory_v1_v5_2_stage_preflight_v1',p_review_report_sha256,
      p_stage_bundle_sha256,review_report_value,stage_bundle_value,
      p_repository_commit,p_auto_link_count,
      p_manual_review_count,p_deferred_resolution_count,p_rejected_count,
      p_blocking_code_count
    );
  END IF;
  RETURN QUERY SELECT p_route_event_id,p_packet_id,
    'manual_review_artifact_ready'::text,'applied'::text;
END
$function$;
ALTER FUNCTION memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer) OWNER TO memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer) FROM brains_app;
GRANT EXECUTE ON FUNCTION memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer) TO brains_app;
