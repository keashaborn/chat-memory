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
  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  replayed memory.v5_2_local_packet_route_event%ROWTYPE;
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

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_2_packet_route',actor::text,p_packet_id::text
  ),0));

  SELECT value.* INTO replayed
  FROM memory.v5_2_local_packet_route_event AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id OR value.packet_id=p_packet_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.route_event_id<>p_route_event_id
       OR replayed.packet_id<>p_packet_id
       OR replayed.route<>'manual_review_artifact_ready'
       OR replayed.reason_code<>'reviewable_relational_packet_v5_2'
       OR replayed.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR replayed.routing_basis_sha256<>p_expected_routing_basis_sha256
       OR replayed.review_id<>p_review_id
       OR replayed.request_id<>p_request_id
       OR replayed.review_report_sha256<>p_review_report_sha256
       OR replayed.stage_bundle_sha256<>p_stage_bundle_sha256
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

  SELECT value.* INTO STRICT packet
  FROM memory.evidence_extraction_packet_v5_local AS value
  WHERE value.owner_user_id=actor
    AND value.packet_id=p_packet_id;
  IF p_auto_link_count+p_manual_review_count+p_deferred_resolution_count
       +p_rejected_count<>packet.entity_mention_count THEN
    RAISE EXCEPTION 'V5.2 review resolution counts do not match packet'
      USING ERRCODE='23514';
  END IF;

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

  RETURN QUERY SELECT p_route_event_id,p_packet_id,
    'manual_review_artifact_ready'::text,'applied'::text;
END
$function$;
ALTER FUNCTION memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer) OWNER TO memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer) FROM brains_app;
GRANT EXECUTE ON FUNCTION memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer) TO brains_app;
