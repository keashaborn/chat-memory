CREATE OR REPLACE FUNCTION memory.persist_owner_evidence_extraction_packet_v5(p_operation_id uuid, p_packet_id uuid, p_job_id uuid, p_lease_token uuid, p_worker_id text, p_expected_content_sha256 text, p_provider_id text, p_provider_version text, p_provider_model_sha256 text, p_provider_output_sha256 text, p_normalized_packet_sha256 text, p_normalized_packet jsonb, p_manual_review_required boolean, p_external_model_calls integer, p_project_binding_event_id uuid DEFAULT NULL::uuid)
RETURNS TABLE(packet_id uuid, job_id uuid, status text, validator_packet_sha256 text, packet_storage_sha256 text, apply_outcome text)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  calculated_storage_sha256 text;
  current_job memory.evidence_extraction_job%ROWTYPE;
  evidence_record memory.evidence%ROWTYPE;
  replayed memory.evidence_extraction_packet_v5%ROWTYPE;
  binding_record record;
  evidence_thread_id uuid;
  entity_count integer;
  observation_count integer;
  comparison_count integer;
  deferral_count integer;
  project_count integer;
  resolved_project_count integer;
  project_key_count integer;
  project_key_value text;
  final_summary jsonb;
  next_result jsonb;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5 extraction persistence requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_packet_id IS NULL
     OR p_job_id IS NULL
     OR p_lease_token IS NULL
     OR p_worker_id IS NULL
     OR btrim(p_worker_id)=''
     OR length(p_worker_id)>500
     OR p_expected_content_sha256 IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_provider_id NOT IN ('openai_responses','synthetic_fixture')
     OR p_provider_version IS NULL
     OR p_provider_version !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
     OR p_provider_model_sha256 IS NULL
     OR p_provider_model_sha256 !~ '^[0-9a-f]{64}$'
     OR p_provider_output_sha256 IS NULL
     OR p_provider_output_sha256 !~ '^[0-9a-f]{64}$'
     OR p_normalized_packet_sha256 IS NULL
     OR p_normalized_packet_sha256 !~ '^[0-9a-f]{64}$'
     OR p_normalized_packet IS NULL
     OR jsonb_typeof(p_normalized_packet)<>'object'
     OR pg_column_size(p_normalized_packet)>262144
     OR p_manual_review_required IS NULL
     OR p_external_model_calls IS NULL
     OR p_external_model_calls NOT BETWEEN 0 AND 1 THEN
    RAISE EXCEPTION 'V5 extraction persistence inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  calculated_storage_sha256 := encode(
    public.digest(convert_to(p_normalized_packet::text,'UTF8'),'sha256'),
    'hex'
  );
  IF p_normalized_packet->>'contract_version'
       IS DISTINCT FROM 'memory_v1_relational_extraction_v5'
     OR p_normalized_packet->>'predicate_registry_version'
       IS DISTINCT FROM 'memory_predicate_registry_v5'
     OR p_normalized_packet @? '$.**.owner_user_id'
     OR p_normalized_packet @? '$.**.vantage_id'
     OR jsonb_typeof(p_normalized_packet->'entity_mentions')<>'array'
     OR jsonb_typeof(p_normalized_packet->'observations')<>'array'
     OR jsonb_typeof(p_normalized_packet->'comparison_hints')<>'array'
     OR jsonb_typeof(p_normalized_packet->'deferrals')<>'array'
     OR jsonb_typeof(p_normalized_packet->'packet_findings')<>'array' THEN
    RAISE EXCEPTION 'normalized V5 packet contract is invalid'
      USING ERRCODE='23514';
  END IF;

  entity_count := jsonb_array_length(p_normalized_packet->'entity_mentions');
  observation_count := jsonb_array_length(p_normalized_packet->'observations');
  comparison_count := jsonb_array_length(p_normalized_packet->'comparison_hints');
  deferral_count := jsonb_array_length(p_normalized_packet->'deferrals');
  IF entity_count>24 OR observation_count>32
     OR comparison_count>32 OR deferral_count>32 THEN
    RAISE EXCEPTION 'normalized V5 packet exceeds bounded counts'
      USING ERRCODE='23514';
  END IF;
  IF (
       SELECT count(DISTINCT item->>'entity_ref')
       FROM jsonb_array_elements(
         p_normalized_packet->'entity_mentions'
       ) AS item
     )<>entity_count
     OR (
       SELECT count(DISTINCT item->>'observation_ref')
       FROM jsonb_array_elements(
         p_normalized_packet->'observations'
       ) AS item
     )<>observation_count
     OR EXISTS (
       SELECT 1
       FROM jsonb_array_elements(
         p_normalized_packet->'observations'
       ) AS observation
       WHERE NOT EXISTS (
         SELECT 1
         FROM jsonb_array_elements(
           p_normalized_packet->'entity_mentions'
         ) AS mention
         WHERE mention->>'entity_ref'
           =observation->>'subject_entity_ref'
       )
     ) THEN
    RAISE EXCEPTION 'normalized V5 packet references are invalid'
      USING ERRCODE='23514';
  END IF;

  SELECT packet.* INTO replayed
  FROM memory.evidence_extraction_packet_v5 AS packet
  WHERE packet.owner_user_id=actor
    AND (
      packet.operation_id=p_operation_id
      OR packet.job_id=p_job_id
    )
  ORDER BY (packet.operation_id=p_operation_id) DESC
  LIMIT 1;
  IF FOUND THEN
    IF replayed.packet_id<>p_packet_id
       OR replayed.job_id<>p_job_id
       OR replayed.evidence_content_sha256<>p_expected_content_sha256
       OR replayed.provider_id<>p_provider_id
       OR replayed.provider_version<>p_provider_version
       OR replayed.provider_model_sha256<>p_provider_model_sha256
       OR replayed.provider_output_sha256<>p_provider_output_sha256
       OR replayed.validator_packet_sha256<>p_normalized_packet_sha256
       OR replayed.packet_storage_sha256<>calculated_storage_sha256
       OR replayed.normalized_packet<>p_normalized_packet
       OR replayed.project_binding_event_id
          IS DISTINCT FROM p_project_binding_event_id THEN
      RAISE EXCEPTION 'V5 extraction persistence replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      replayed.packet_id,replayed.job_id,'review_required'::text,
      replayed.validator_packet_sha256,
      replayed.packet_storage_sha256,'replayed'::text;
    RETURN;
  END IF;

  SELECT job.* INTO current_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND
     OR current_job.status<>'processing'
     OR current_job.route<>'relational_extraction'
     OR current_job.lease_token<>p_lease_token
     OR current_job.worker_id IS DISTINCT FROM p_worker_id
     OR current_job.lease_expires_at<=clock_timestamp()
     OR current_job.evidence_content_sha256
        IS DISTINCT FROM p_expected_content_sha256 THEN
    RAISE EXCEPTION 'V5 extraction lease or content binding is invalid'
      USING ERRCODE='23514';
  END IF;

  SELECT evidence.* INTO evidence_record
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=current_job.evidence_id;
  IF NOT FOUND
     OR evidence_record.source_system<>'public.chat_log'
     OR evidence_record.content_sha256<>p_expected_content_sha256
     OR p_normalized_packet#>>'{source_envelope,job_id}'
        IS DISTINCT FROM p_job_id::text
     OR p_normalized_packet#>>'{source_envelope,source_system}'
        IS DISTINCT FROM evidence_record.source_system
     OR p_normalized_packet#>>'{source_envelope,source_external_id}'
        IS DISTINCT FROM evidence_record.external_id
     OR p_normalized_packet#>>'{source_envelope,source_sha256}'
        IS DISTINCT FROM evidence_record.content_sha256
     OR (p_normalized_packet#>>'{source_envelope,source_recorded_at}')::timestamptz
        IS DISTINCT FROM evidence_record.recorded_at THEN
    RAISE EXCEPTION 'normalized V5 packet source binding is invalid'
      USING ERRCODE='23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM jsonb_array_elements(p_normalized_packet->'observations') AS item
    WHERE NOT memory.v5_project_scope_valid(item->'project_scope')
       OR CASE
         WHEN item->>'projection_class'='project_knowledge' THEN
           item#>>'{project_scope,state}' NOT IN ('resolved','unresolved')
         ELSE
           item#>>'{project_scope,state}'<>'not_applicable'
       END
  ) THEN
    RAISE EXCEPTION 'normalized V5 project scope is invalid'
      USING ERRCODE='23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM jsonb_array_elements(
      p_normalized_packet->'observations'
    ) AS observation
    LEFT JOIN LATERAL (
      SELECT item AS mention
      FROM jsonb_array_elements(
        p_normalized_packet->'entity_mentions'
      ) AS item
      WHERE item->>'entity_ref'=observation->>'subject_entity_ref'
      LIMIT 1
    ) AS subject ON true
    WHERE observation->>'projection_class'='project_knowledge'
      AND (
        subject.mention IS NULL
        OR subject.mention->>'entity_type'<>'project'
        OR (
          observation#>>'{project_scope,state}'='resolved'
          AND NOT (
            (
              observation#>>'{project_scope,component_key}' IS NULL
              AND
              subject.mention->>'mention_kind'='anonymous'
              AND subject.mention->>'relationship_role'
                ='project:current_thread'
              AND subject.mention->'name_text'='null'::jsonb
            )
            OR
            (
              observation#>>'{project_scope,component_key}' IS NULL
              AND
              subject.mention->>'mention_kind'='named'
              AND btrim(
                regexp_replace(
                  lower(subject.mention->>'name_text'),
                  '[^a-z0-9]+','-','g'
                ),
                '-'
              )=btrim(
                regexp_replace(
                  lower(observation#>>'{project_scope,project_key}'),
                  '[^a-z0-9]+','-','g'
                ),
                '-'
              )
            )
            OR
            (
              observation#>>'{project_scope,component_key}' IS NOT NULL
              AND observation#>>'{project_scope,binding_source}'
                    ='trusted_component_registry'
              AND subject.mention->>'mention_kind'='named'
              AND coalesce(btrim(subject.mention->>'name_text'),'')<>''
              AND EXISTS (
                SELECT 1
                FROM memory.project_space AS project
                JOIN memory.project_component_v5 AS component
                  ON component.owner_user_id=project.owner_user_id
                 AND component.project_id=project.project_id
                JOIN memory.project_component_alias_v5 AS alias
                  ON alias.owner_user_id=component.owner_user_id
                 AND alias.project_id=component.project_id
                 AND alias.component_id=component.component_id
                WHERE project.owner_user_id=actor
                  AND project.project_key
                        =observation#>>'{project_scope,project_key}'
                  AND component.component_key
                        =observation#>>'{project_scope,component_key}'
                  AND alias.normalized_alias
                        =memory.normalize_project_component_alias_v5(
                          subject.mention->>'name_text'
                        )
              )
            )
          )
        )
      )
  ) THEN
    RAISE EXCEPTION 'trusted project subject conflicts with thread binding'
      USING ERRCODE='23514';
  END IF;

  SELECT
    count(*) FILTER (
      WHERE item->>'projection_class'='project_knowledge'
    ),
    count(*) FILTER (
      WHERE item->>'projection_class'='project_knowledge'
        AND item#>>'{project_scope,state}'='resolved'
    ),
    count(DISTINCT item#>>'{project_scope,project_key}') FILTER (
      WHERE item->>'projection_class'='project_knowledge'
        AND item#>>'{project_scope,state}'='resolved'
    ),
    min(item#>>'{project_scope,project_key}') FILTER (
      WHERE item->>'projection_class'='project_knowledge'
        AND item#>>'{project_scope,state}'='resolved'
    )
  INTO project_count,resolved_project_count,project_key_count,project_key_value
  FROM jsonb_array_elements(p_normalized_packet->'observations') AS item;

  IF resolved_project_count>0 THEN
    IF resolved_project_count<>project_count
       OR project_key_count<>1
       OR p_project_binding_event_id IS NULL
       OR jsonb_typeof(evidence_record.metadata->'thread_id')<>'string'
       OR evidence_record.metadata->>'thread_id'
          !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' THEN
      RAISE EXCEPTION 'trusted project scope lacks one backend binding'
        USING ERRCODE='23514';
    END IF;
    evidence_thread_id := (evidence_record.metadata->>'thread_id')::uuid;
    SELECT binding.* INTO binding_record
    FROM memory.current_project_thread_binding_v5 AS binding
    WHERE binding.owner_user_id=actor
      AND binding.thread_id=evidence_thread_id;
    IF NOT FOUND
       OR binding_record.binding_event_id<>p_project_binding_event_id
       OR binding_record.project_key<>project_key_value THEN
      RAISE EXCEPTION 'trusted project binding changed or conflicts'
        USING ERRCODE='23514';
    END IF;
  ELSIF p_project_binding_event_id IS NOT NULL THEN
    RAISE EXCEPTION 'unused project binding event is forbidden'
      USING ERRCODE='23514';
  END IF;

  final_summary := jsonb_build_object(
    'contract_version','memory_v1_evidence_extraction_packet_summary_v5',
    'packet_id',p_packet_id,
    'provider_id',p_provider_id,
    'provider_version',p_provider_version,
    'provider_model_sha256',p_provider_model_sha256,
    'provider_output_sha256',p_provider_output_sha256,
    'validator_packet_sha256',p_normalized_packet_sha256,
    'packet_storage_sha256',calculated_storage_sha256,
    'manual_review_required',p_manual_review_required,
    'external_model_calls',p_external_model_calls,
    'project_binding_event_id',p_project_binding_event_id,
    'counts',jsonb_build_object(
      'entity_mentions',entity_count,
      'observations',observation_count,
      'comparison_hints',comparison_count,
      'deferrals',deferral_count
    ),
    'write_counts',jsonb_build_object(
      'candidates',0,'claims',0,'staging',0,'qdrant',0,'prompt_influence',0
    )
  );
  next_result := jsonb_set(
    current_job.result,
    '{final}',
    jsonb_build_object(
      'status','review_required',
      'sha256',calculated_storage_sha256,
      'payload',final_summary
    ),
    true
  );
  IF pg_column_size(next_result)>32768 THEN
    RAISE EXCEPTION 'sanitized V5 job summary exceeds queue budget'
      USING ERRCODE='22023';
  END IF;

  INSERT INTO memory.evidence_extraction_packet_v5(
    packet_id,owner_user_id,operation_id,job_id,evidence_id,
    evidence_content_sha256,provider_id,provider_version,
    provider_model_sha256,
    provider_output_sha256,validator_packet_sha256,
    packet_storage_sha256,normalized_packet,
    manual_review_required,external_model_calls,project_binding_event_id,
    entity_mention_count,observation_count,comparison_hint_count,
    deferral_count
  ) VALUES (
    p_packet_id,actor,p_operation_id,p_job_id,current_job.evidence_id,
    p_expected_content_sha256,p_provider_id,p_provider_version,
    p_provider_model_sha256,
    p_provider_output_sha256,p_normalized_packet_sha256,
    calculated_storage_sha256,p_normalized_packet,
    p_manual_review_required,p_external_model_calls,
    p_project_binding_event_id,entity_count,observation_count,
    comparison_count,deferral_count
  );

  UPDATE memory.evidence_extraction_job AS job
  SET
    status='review_required',
    lease_token=NULL,
    lease_expires_at=NULL,
    last_error=NULL,
    result=next_result
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id;

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,from_status,to_status,
    actor_type,actor_ref,details
  ) VALUES (
    actor,p_job_id,p_operation_id,'review_required','processing',
    'review_required','worker',p_worker_id,
    jsonb_build_object(
      'packet_id',p_packet_id,
      'validator_packet_sha256',p_normalized_packet_sha256,
      'packet_storage_sha256',calculated_storage_sha256,
      'provider_output_sha256',p_provider_output_sha256,
      'provider_model_sha256',p_provider_model_sha256,
      'project_binding_event_id',p_project_binding_event_id,
      'entity_mention_count',entity_count,
      'observation_count',observation_count,
      'comparison_hint_count',comparison_count,
      'deferral_count',deferral_count,
      'external_model_calls',p_external_model_calls
    )
  );

  RETURN QUERY SELECT
    p_packet_id,p_job_id,'review_required'::text,
    p_normalized_packet_sha256,calculated_storage_sha256,'applied'::text;
END
$function$;
ALTER FUNCTION memory.persist_owner_evidence_extraction_packet_v5(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,jsonb,boolean,integer,uuid) OWNER TO memory_v5_extraction_maintainer;
REVOKE ALL ON FUNCTION memory.persist_owner_evidence_extraction_packet_v5(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,jsonb,boolean,integer,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.persist_owner_evidence_extraction_packet_v5(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,jsonb,boolean,integer,uuid) FROM brains_app;
GRANT EXECUTE ON FUNCTION memory.persist_owner_evidence_extraction_packet_v5(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,jsonb,boolean,integer,uuid) TO brains_app;
