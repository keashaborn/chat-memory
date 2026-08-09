CREATE OR REPLACE FUNCTION memory.preflight_owner_openai_review_admission_v1(p_route_event_id uuid, p_expected_stage_bundle_sha256 text, p_reviewer_type text, p_reviewer_ref text, p_reason text, p_reason_codes jsonb)
RETURNS TABLE(route_event_id uuid, packet_id uuid, job_id uuid, evidence_id uuid, stage_request_id uuid, observation_ref text, review_report_sha256 text, stage_bundle_sha256 text, extraction_packet_sha256 text, resolution_packet_sha256 text, packet_storage_sha256 text, admission_manifest_sha256 text)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  source memory.v5_2_openai_packet_route_event%ROWTYPE;
  bundle jsonb;
  extraction jsonb;
  resolution jsonb;
  observation jsonb;
  observation_ref text;
  subject_entity_ref text;
  normalized_reason_codes jsonb;
  manifest text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'OpenAI review admission preflight requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required' USING ERRCODE='42501';
  END IF;
  IF p_route_event_id IS NULL
     OR p_expected_stage_bundle_sha256 !~ '^[0-9a-f]{64}$'
     OR p_reviewer_type NOT IN ('user','admin')
     OR btrim(COALESCE(p_reviewer_ref,''))=''
     OR octet_length(p_reviewer_ref)>500
     OR btrim(COALESCE(p_reason,''))=''
     OR octet_length(p_reason)>2000
     OR jsonb_typeof(p_reason_codes)<>'array'
     OR jsonb_array_length(p_reason_codes) NOT BETWEEN 1 AND 20
     OR EXISTS (
       SELECT 1 FROM jsonb_array_elements(p_reason_codes) AS value
       WHERE jsonb_typeof(value)<>'string'
          OR btrim(value#>>'{}')=''
          OR octet_length(value#>>'{}')>120
     ) THEN
    RAISE EXCEPTION 'OpenAI review admission inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  SELECT value.* INTO source
  FROM memory.v5_2_openai_packet_route_event AS value
  WHERE value.owner_user_id=actor
    AND value.route_event_id=p_route_event_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped OpenAI review route is absent'
      USING ERRCODE='P0002';
  END IF;
  bundle:=source.stage_bundle;
  extraction:=(bundle->>'extraction_packet_text')::jsonb;
  resolution:=(bundle->>'resolution_packet_text')::jsonb;
  IF source.route<>'manual_review_artifact_ready'
     OR source.reason_code<>'reviewable_relational_packet_v5_2'
     OR source.review_contract<>'memory_v1_v5_2_openai_packet_review_v1'
     OR source.bundle_contract<>'memory_v1_v5_2_stage_preflight_v1'
     OR source.stage_bundle_sha256<>p_expected_stage_bundle_sha256
     OR source.review_report->>'review_disposition'<>'manual_review_required'
     OR source.observation_count<>1
     OR source.comparison_hint_count<>0 OR source.deferral_count<>0
     OR source.manual_review_count<>0
     OR source.deferred_resolution_count<>0 OR source.rejected_count<>0
     OR source.blocking_code_count<>0
     OR source.auto_link_count<>source.entity_mention_count
     OR bundle->>'contract_version'<>'memory_v1_v5_2_stage_preflight_v1'
     OR bundle->>'owner_user_id'<>actor::text
     OR bundle->>'evidence_id'<>source.evidence_id::text
     OR (bundle->>'database_writes')::integer<>0
     OR (bundle->>'qdrant_writes')::integer<>0
     OR (bundle->>'external_model_calls')::integer<>0
     OR (bundle->>'authorized_stage')::boolean IS DISTINCT FROM false
     OR extraction->>'contract_version'<>'memory_v1_relational_extraction_v5_2'
     OR resolution->>'contract_version'<>'memory_v1_entity_resolution_review_v5_2'
     OR jsonb_array_length(extraction->'observations')<>1
     OR jsonb_array_length(extraction->'comparison_hints')<>0
     OR jsonb_array_length(extraction->'deferrals')<>0
     OR jsonb_array_length(resolution->'resolutions')<>source.entity_mention_count
     OR encode(public.digest(convert_to(bundle->>'extraction_packet_text','UTF8'),'sha256'),'hex')
          <>bundle->>'extraction_packet_sha256'
     OR encode(public.digest(convert_to(bundle->>'resolution_packet_text','UTF8'),'sha256'),'hex')
          <>bundle->>'resolution_packet_sha256' THEN
    RAISE EXCEPTION 'OpenAI review route is not single-observation admission eligible'
      USING ERRCODE='23514';
  END IF;
  SELECT value INTO observation
  FROM jsonb_array_elements(extraction->'observations') AS value;
  observation_ref:=observation->>'observation_ref';
  subject_entity_ref:=observation->>'subject_entity_ref';
  IF observation_ref !~ '^o[0-9]{2}$'
     OR subject_entity_ref !~ '^e[0-9]{2}$'
     OR observation->>'predicate'<>'stance.reported'
     OR observation->>'projection_class'<>'reported_stance'
     OR observation->>'surface_policy'<>'relevant_recall_or_explicit_recall'
     OR observation->>'modality'<>'reported_belief'
     OR observation->>'polarity'<>'affirmed'
     OR observation->>'predicate_registry_status'<>'governed' THEN
    RAISE EXCEPTION 'OpenAI review observation is outside the governed claim MVP boundary'
      USING ERRCODE='23514';
  END IF;
  SELECT jsonb_agg(value ORDER BY value#>>'{}') INTO normalized_reason_codes
  FROM jsonb_array_elements(p_reason_codes) AS value;
  manifest:=encode(public.digest(convert_to(jsonb_build_object(
    'contract_version','memory_v1_openai_review_admission_authorization_v1',
    'owner_user_id',actor::text,
    'route_event_id',source.route_event_id::text,
    'packet_id',source.packet_id::text,
    'job_id',source.job_id::text,
    'evidence_id',source.evidence_id::text,
    'stage_request_id',bundle->>'request_id',
    'observation_ref',observation_ref,
    'subject_entity_ref',subject_entity_ref,
    'review_report_sha256',source.review_report_sha256,
    'stage_bundle_sha256',source.stage_bundle_sha256,
    'extraction_packet_sha256',bundle->>'extraction_packet_sha256',
    'resolution_packet_sha256',bundle->>'resolution_packet_sha256',
    'packet_storage_sha256',source.packet_storage_sha256,
    'reviewer_type',p_reviewer_type,
    'reviewer_ref',btrim(p_reviewer_ref),
    'reason',btrim(p_reason),
    'reason_codes',normalized_reason_codes,
    'policy_version','memory_v1_openai_review_admission_policy_v1'
  )::text,'UTF8'),'sha256'),'hex');
  RETURN QUERY SELECT source.route_event_id,source.packet_id,source.job_id,
    source.evidence_id,(bundle->>'request_id')::uuid,observation_ref,
    source.review_report_sha256,source.stage_bundle_sha256,
    bundle->>'extraction_packet_sha256',bundle->>'resolution_packet_sha256',
    source.packet_storage_sha256,manifest;
END
$function$;

ALTER FUNCTION memory.preflight_owner_openai_review_admission_v1(uuid,text,text,text,text,jsonb) OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION memory.preflight_owner_openai_review_admission_v1(uuid,text,text,text,text,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.preflight_owner_openai_review_admission_v1(uuid,text,text,text,text,jsonb) FROM brains_app;
REVOKE ALL ON FUNCTION memory.preflight_owner_openai_review_admission_v1(uuid,text,text,text,text,jsonb) FROM memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.preflight_owner_openai_review_admission_v1(uuid,text,text,text,text,jsonb) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_owner_openai_review_admission_v1(uuid,text,text,text,text,jsonb) TO memory_v5_writer;
