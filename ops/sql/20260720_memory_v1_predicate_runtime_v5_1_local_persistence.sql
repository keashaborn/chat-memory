BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $preflight$
BEGIN
  IF current_user<>'sage'
     OR to_regrole('memory_v5_local_inference_maintainer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('public.digest(bytea,text)') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.evidence') IS NULL THEN
    RAISE EXCEPTION 'V5.1 local persistence prerequisites are absent';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM memory.predicate_registry_version
    WHERE registry_version='memory_predicate_registry_v5_1'
  ) THEN
    RAISE EXCEPTION 'V5.1 predicate registry is not installed';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.persist_owner_v5_1_local_packet_v1(
  p_operation_id uuid,
  p_packet_id uuid,
  p_job_id uuid,
  p_lease_token uuid,
  p_worker_id text,
  p_expected_content_sha256 text,
  p_provider_version text,
  p_provider_model_sha256 text,
  p_model_file_sha256 text,
  p_runtime_revision_sha256 text,
  p_policy_compiler_sha256 text,
  p_provider_output_sha256 text,
  p_normalized_packet_sha256 text,
  p_normalized_packet jsonb,
  p_manual_review_required boolean,
  p_local_model_calls integer
)
RETURNS TABLE(
  packet_id uuid,
  job_id uuid,
  status text,
  validator_packet_sha256 text,
  packet_storage_sha256 text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  calculated_storage_sha256 text;
  current_job memory.evidence_extraction_job%ROWTYPE;
  evidence_record memory.evidence%ROWTYPE;
  replayed memory.evidence_extraction_packet_v5_local%ROWTYPE;
  entity_count integer;
  observation_count integer;
  comparison_count integer;
  deferral_count integer;
  final_summary jsonb;
  next_result jsonb;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5.1 local packet persistence requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_packet_id IS NULL OR p_job_id IS NULL
     OR p_lease_token IS NULL OR p_worker_id IS NULL
     OR btrim(p_worker_id)='' OR length(p_worker_id)>500
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_provider_version !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
     OR p_provider_version <> 'v1'
     OR p_provider_model_sha256 !~ '^[0-9a-f]{64}$'
     OR p_model_file_sha256 !~ '^[0-9a-f]{64}$'
     OR p_runtime_revision_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_compiler_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_compiler_sha256 <> encode(public.digest(convert_to(
       'memory_v1_relationship_policy_compiler_v14','UTF8'
     ),'sha256'),'hex')
     OR p_provider_output_sha256 !~ '^[0-9a-f]{64}$'
     OR p_normalized_packet_sha256 !~ '^[0-9a-f]{64}$'
     OR p_normalized_packet IS NULL
     OR jsonb_typeof(p_normalized_packet)<>'object'
     OR pg_column_size(p_normalized_packet)>262144
     OR p_manual_review_required IS NULL
     OR p_local_model_calls NOT BETWEEN 0 AND 1 THEN
    RAISE EXCEPTION 'V5.1 local packet persistence inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  calculated_storage_sha256 := encode(
    public.digest(convert_to(p_normalized_packet::text,'UTF8'),'sha256'),'hex'
  );
  IF p_normalized_packet->>'contract_version'
       IS DISTINCT FROM 'memory_v1_relational_extraction_v5_1'
     OR p_normalized_packet->>'predicate_registry_version'
       IS DISTINCT FROM 'memory_predicate_registry_v5_1'
     OR p_normalized_packet @? '$.**.owner_user_id'
     OR p_normalized_packet @? '$.**.vantage_id'
     OR jsonb_typeof(p_normalized_packet->'entity_mentions')<>'array'
     OR jsonb_typeof(p_normalized_packet->'observations')<>'array'
     OR jsonb_typeof(p_normalized_packet->'comparison_hints')<>'array'
     OR jsonb_typeof(p_normalized_packet->'deferrals')<>'array'
     OR jsonb_typeof(p_normalized_packet->'packet_findings')<>'array' THEN
    RAISE EXCEPTION 'normalized local V5.1 packet contract is invalid'
      USING ERRCODE='23514';
  END IF;

  entity_count := jsonb_array_length(p_normalized_packet->'entity_mentions');
  observation_count := jsonb_array_length(p_normalized_packet->'observations');
  comparison_count := jsonb_array_length(p_normalized_packet->'comparison_hints');
  deferral_count := jsonb_array_length(p_normalized_packet->'deferrals');
  IF entity_count>24 OR observation_count>32
     OR comparison_count>32 OR deferral_count>32 THEN
    RAISE EXCEPTION 'normalized local V5.1 packet exceeds bounded counts'
      USING ERRCODE='23514';
  END IF;
  IF (
       SELECT count(DISTINCT item->>'entity_ref')
       FROM jsonb_array_elements(p_normalized_packet->'entity_mentions') AS item
     )<>entity_count
     OR (
       SELECT count(DISTINCT item->>'observation_ref')
       FROM jsonb_array_elements(p_normalized_packet->'observations') AS item
     )<>observation_count
     OR EXISTS (
       SELECT 1
       FROM jsonb_array_elements(p_normalized_packet->'observations') AS obs
       WHERE NOT EXISTS (
         SELECT 1
         FROM jsonb_array_elements(p_normalized_packet->'entity_mentions') AS ent
         WHERE ent->>'entity_ref'=obs->>'subject_entity_ref'
       ) OR (
         obs#>>'{object,kind}'='entity' AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(p_normalized_packet->'entity_mentions') AS ent
           WHERE ent->>'entity_ref'=obs#>>'{object,entity_ref}'
         )
       )
     ) THEN
    RAISE EXCEPTION 'normalized local V5.1 packet references are invalid'
      USING ERRCODE='23514';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM jsonb_array_elements(p_normalized_packet->'observations') AS obs
    WHERE obs->>'projection_class'='project_knowledge'
      AND obs#>>'{project_scope,state}'<>'unresolved'
  ) THEN
    RAISE EXCEPTION 'local V5.1 project knowledge must remain unresolved'
      USING ERRCODE='23514';
  END IF;

  SELECT packet.* INTO replayed
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id=actor
    AND (packet.operation_id=p_operation_id OR packet.job_id=p_job_id)
  ORDER BY (packet.operation_id=p_operation_id) DESC
  LIMIT 1;
  IF FOUND THEN
    IF replayed.packet_id<>p_packet_id OR replayed.job_id<>p_job_id
       OR replayed.evidence_content_sha256<>p_expected_content_sha256
       OR replayed.provider_version<>p_provider_version
       OR replayed.provider_model_sha256<>p_provider_model_sha256
       OR replayed.model_file_sha256<>p_model_file_sha256
       OR replayed.runtime_revision_sha256<>p_runtime_revision_sha256
       OR replayed.policy_compiler_sha256<>p_policy_compiler_sha256
       OR replayed.provider_output_sha256<>p_provider_output_sha256
       OR replayed.validator_packet_sha256<>p_normalized_packet_sha256
       OR replayed.packet_storage_sha256<>calculated_storage_sha256
       OR replayed.normalized_packet<>p_normalized_packet
       OR replayed.local_model_calls<>p_local_model_calls THEN
      RAISE EXCEPTION 'V5.1 local packet replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.packet_id,replayed.job_id,
      'review_required'::text,replayed.validator_packet_sha256,
      replayed.packet_storage_sha256,'replayed'::text;
    RETURN;
  END IF;

  SELECT job.* INTO current_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND OR current_job.status<>'processing'
     OR current_job.route<>'relational_extraction'
     OR current_job.lease_token<>p_lease_token
     OR current_job.worker_id IS DISTINCT FROM p_worker_id
     OR current_job.lease_expires_at<=clock_timestamp()
     OR current_job.evidence_content_sha256
        IS DISTINCT FROM p_expected_content_sha256 THEN
    RAISE EXCEPTION 'V5.1 local packet lease or content binding is invalid'
      USING ERRCODE='23514';
  END IF;

  SELECT evidence.* INTO evidence_record
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=current_job.evidence_id;
  IF NOT FOUND OR evidence_record.source_system<>'public.chat_log'
     OR evidence_record.content_sha256<>p_expected_content_sha256
     OR p_normalized_packet#>>'{source_envelope,job_id}'
        IS DISTINCT FROM p_job_id::text
     OR p_normalized_packet#>>'{source_envelope,source_system}'
        IS DISTINCT FROM evidence_record.source_system
     OR p_normalized_packet#>>'{source_envelope,source_external_id}'
        IS DISTINCT FROM (CASE
          WHEN evidence_record.external_id ~*
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN lower(evidence_record.external_id)
          ELSE evidence_record.evidence_id::text
        END)
     OR p_normalized_packet#>>'{source_envelope,source_sha256}'
        IS DISTINCT FROM evidence_record.content_sha256
     OR (p_normalized_packet#>>'{source_envelope,source_recorded_at}')::timestamptz
        IS DISTINCT FROM evidence_record.recorded_at THEN
    RAISE EXCEPTION 'normalized local V5.1 packet source binding is invalid'
      USING ERRCODE='23514';
  END IF;

  final_summary := jsonb_build_object(
    'contract_version','memory_v1_evidence_extraction_packet_summary_v5_1_local',
    'packet_id',p_packet_id,'provider_id','local_llama_cpp',
    'provider_version',p_provider_version,
    'provider_model_sha256',p_provider_model_sha256,
    'model_file_sha256',p_model_file_sha256,
    'runtime_revision_sha256',p_runtime_revision_sha256,
    'policy_compiler_sha256',p_policy_compiler_sha256,
    'provider_output_sha256',p_provider_output_sha256,
    'validator_packet_sha256',p_normalized_packet_sha256,
    'packet_storage_sha256',calculated_storage_sha256,
    'manual_review_required',p_manual_review_required,
    'local_model_calls',p_local_model_calls,'external_model_calls',0,
    'write_counts',jsonb_build_object(
      'candidates',0,'claims',0,'staging',0,'qdrant',0,'prompt_influence',0
    )
  );
  next_result := jsonb_set(
    current_job.result,'{final}',jsonb_build_object(
      'status','review_required','sha256',calculated_storage_sha256,
      'payload',final_summary
    ),true
  );
  IF pg_column_size(next_result)>32768 THEN
    RAISE EXCEPTION 'sanitized local V5.1 job summary exceeds queue budget'
      USING ERRCODE='22023';
  END IF;

  INSERT INTO memory.evidence_extraction_packet_v5_local(
    packet_id,owner_user_id,operation_id,job_id,evidence_id,
    evidence_content_sha256,provider_id,provider_version,
    provider_model_sha256,model_file_sha256,runtime_revision_sha256,
    policy_compiler_sha256,provider_output_sha256,
    validator_packet_sha256,packet_storage_sha256,normalized_packet,
    manual_review_required,local_model_calls,external_model_calls,
    entity_mention_count,observation_count,comparison_hint_count,deferral_count
  ) VALUES (
    p_packet_id,actor,p_operation_id,p_job_id,current_job.evidence_id,
    p_expected_content_sha256,'local_llama_cpp',p_provider_version,
    p_provider_model_sha256,p_model_file_sha256,p_runtime_revision_sha256,
    p_policy_compiler_sha256,p_provider_output_sha256,
    p_normalized_packet_sha256,calculated_storage_sha256,p_normalized_packet,
    p_manual_review_required,p_local_model_calls,0,
    entity_count,observation_count,comparison_count,deferral_count
  );

  UPDATE memory.evidence_extraction_job AS job
  SET status='review_required',lease_token=NULL,lease_expires_at=NULL,
      last_error=NULL,result=next_result
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
      'local_model_calls',p_local_model_calls,'external_model_calls',0
    )
  );

  RETURN QUERY SELECT p_packet_id,p_job_id,'review_required'::text,
    p_normalized_packet_sha256,calculated_storage_sha256,'applied'::text;
END
$function$;

ALTER FUNCTION memory.persist_owner_v5_1_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) OWNER TO memory_v5_local_inference_maintainer;

REVOKE ALL ON FUNCTION memory.persist_owner_v5_1_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;

GRANT EXECUTE ON FUNCTION memory.persist_owner_v5_1_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) TO brains_app;

COMMIT;
