BEGIN;

CREATE OR REPLACE FUNCTION memory.classify_v5_local_inference_outcome_v2(
  p_rejection_code text
)
RETURNS TABLE(
  outcome_class text,
  normalized_reason_code text,
  disposition text,
  circuit_impact boolean,
  immediate_open boolean
)
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
BEGIN
  IF p_rejection_code IN (
    'ambiguous_transcription',
    'compound_requires_split',
    'context_coreference_unresolved',
    'context_missing',
    'entity_resolution_review_required',
    'entity_resolution_unresolved',
    'insufficient_durable_evidence',
    'insufficient_evidence',
    'local_validation_rejected',
    'mixed_authorship',
    'nothing_durable_to_stage',
    'ordinary_semantic_rejection',
    'predicate_review_required',
    'project_scope_unresolved',
    'question_only',
    'semantic_validation_rejected',
    'sensitive_manual_review',
    'sensitive_or_ambiguous_review',
    'structured_domain',
    'transient_state',
    'unregistered_predicate'
  ) THEN
    RETURN QUERY SELECT
      'record_terminal'::text,
      CASE p_rejection_code
        WHEN 'context_coreference_unresolved'
          THEN 'context_reference_unresolved'
        WHEN 'local_validation_rejected'
          THEN 'semantic_validation_rejected_legacy'
        WHEN 'insufficient_evidence'
          THEN 'insufficient_durable_evidence'
        WHEN 'sensitive_manual_review'
          THEN 'sensitive_or_ambiguous_review'
        WHEN 'entity_resolution_unresolved'
          THEN 'entity_resolution_review_required'
        WHEN 'unregistered_predicate'
          THEN 'predicate_review_required'
        ELSE p_rejection_code
      END,
      CASE
        WHEN p_rejection_code IN (
          'compound_requires_split',
          'entity_resolution_review_required',
          'entity_resolution_unresolved',
          'mixed_authorship',
          'predicate_review_required',
          'project_scope_unresolved',
          'sensitive_manual_review',
          'sensitive_or_ambiguous_review',
          'unregistered_predicate'
        ) THEN 'review_required'
        WHEN p_rejection_code IN (
          'ambiguous_transcription',
          'context_coreference_unresolved',
          'context_missing'
        ) THEN 'deferred'
        ELSE 'skipped'
      END,
      false,
      false;
    RETURN;
  END IF;

  IF p_rejection_code='local_worker_abandoned' THEN
    RETURN QUERY SELECT
      'neutral'::text,p_rejection_code,NULL::text,false,false;
    RETURN;
  END IF;

  IF p_rejection_code IN ('circuit_open','quota_exhausted') THEN
    RETURN QUERY SELECT
      'control'::text,p_rejection_code,NULL::text,false,false;
    RETURN;
  END IF;

  IF p_rejection_code IN (
    'local_compiler_hash_mismatch',
    'local_model_hash_mismatch',
    'local_owner_scope_violation',
    'local_rls_invariant_failed',
    'local_runtime_hash_mismatch',
    'local_security_invariant_failed'
  ) THEN
    RETURN QUERY SELECT
      'systemic_immediate'::text,p_rejection_code,NULL::text,true,true;
    RETURN;
  END IF;

  IF p_rejection_code IN (
    'duplicate_entity_ref',
    'duplicate_observation_ref',
    'duplicate_reason_codes',
    'duplicate_source_span',
    'call_budget_invalid',
    'calendar_range_invalid',
    'calendar_temporal_basis_mismatch',
    'comparison_observation_ref_unknown',
    'dangling_object_entity_ref',
    'dangling_subject_entity_ref',
    'deferral_budget_exceeded',
    'external_calls_disabled',
    'implicit_source_time_shape_invalid',
    'instant_range_invalid',
    'instant_temporal_basis_mismatch',
    'invalid_reason_code',
    'invalid_structured_output',
    'local_completion_rejected',
    'local_incomplete_response',
    'local_model_alias_mismatch',
    'local_persistence_contract_mismatch',
    'local_persistence_rejected',
    'local_provider_disabled',
    'local_reasoning_content_forbidden',
    'local_response_choice_count_invalid',
    'local_response_content_invalid',
    'local_response_invalid_json',
    'local_response_message_invalid',
    'local_response_metadata_invalid',
    'local_response_shape_invalid',
    'local_response_too_large',
    'local_response_usage_invalid',
    'local_structured_content_invalid',
    'local_structured_enum_invalid',
    'local_structured_extra_field',
    'local_structured_required_field_missing',
    'local_structured_type_invalid',
    'local_validation_internal_error',
    'local_transport_auth_rejected',
    'local_transport_http_rejected',
    'local_transport_rate_limited',
    'local_transport_server_error',
    'local_transport_timeout',
    'local_transport_unavailable',
    'normalized_schema_violation',
    'named_entity_missing_name_text',
    'ownership_namespace_forbidden',
    'predicate_registry_violation',
    'provider_call_budget_exceeded',
    'provider_call_count_invalid',
    'provider_call_count_regressed',
    'provider_version_not_enabled',
    'source_binding_invalid',
    'source_span_out_of_bounds',
    'source_span_quote_ambiguous',
    'source_span_quote_mismatch',
    'self_entity_role_mismatch',
    'temporal_none_shape_invalid',
    'temporal_value_cardinality_invalid',
    'trusted_source_time_asserted_by_provider',
    'month_precision_range_missing',
    'recurring_temporal_basis_mismatch',
    'relative_source_form_invalid',
    'relative_temporal_basis_mismatch',
    'uncatalogued_validator_rejection'
  ) THEN
    RETURN QUERY SELECT
      'systemic_threshold'::text,p_rejection_code,NULL::text,true,false;
    RETURN;
  END IF;

  RETURN QUERY SELECT
    'systemic_unclassified'::text,
    'unclassified_failure'::text,
    NULL::text,
    true,
    true;
END
$function$;

ALTER FUNCTION memory.classify_v5_local_inference_outcome_v2(text)
  OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.classify_v5_local_inference_outcome_v2(text)
  FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;

CREATE TABLE memory.v5_local_inference_outcome_event (
  event_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  job_id uuid NOT NULL,
  completion_event_id uuid NOT NULL,
  rejection_code text NOT NULL,
  outcome_class text NOT NULL,
  disposition text NOT NULL,
  normalized_reason_code text NOT NULL,
  policy_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,event_id),
  UNIQUE (owner_user_id,operation_id),
  UNIQUE (owner_user_id,completion_event_id),
  FOREIGN KEY (owner_user_id,job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id,job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,completion_event_id)
    REFERENCES memory.v5_local_inference_event(owner_user_id,event_id)
    ON DELETE RESTRICT,
  CHECK (rejection_code ~ '^[a-z][a-z0-9_]{1,99}$'),
  CHECK (outcome_class='record_terminal'),
  CHECK (disposition IN ('skipped','deferred','review_required')),
  CHECK (normalized_reason_code ~ '^[a-z][a-z0-9_]{1,99}$'),
  CHECK (policy_version='memory_v1_v5_local_outcome_policy_v2')
);

CREATE INDEX v5_local_inference_outcome_owner_time_idx
  ON memory.v5_local_inference_outcome_event(
    owner_user_id,created_at DESC,event_id DESC
  );

ALTER TABLE memory.v5_local_inference_outcome_event OWNER TO sage;

CREATE TRIGGER v5_local_inference_outcome_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_inference_outcome_event
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

ALTER TABLE memory.v5_local_inference_outcome_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_inference_outcome_event FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.v5_local_inference_outcome_event
  USING (owner_user_id=(SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()));

GRANT SELECT,INSERT ON memory.v5_local_inference_outcome_event
  TO memory_v5_local_inference_maintainer;
REVOKE ALL ON TABLE memory.v5_local_inference_outcome_event
  FROM PUBLIC,brains_app;

CREATE OR REPLACE FUNCTION memory.finalize_owner_v5_local_record_outcome_v2(
  p_operation_id uuid,
  p_job_id uuid,
  p_lease_token uuid,
  p_worker_id text,
  p_expected_content_sha256 text,
  p_completion_event_id uuid,
  p_rejection_code text
)
RETURNS TABLE(
  job_id uuid,
  status text,
  disposition text,
  normalized_reason_code text,
  outcome_event_id uuid,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  current_job memory.evidence_extraction_job%ROWTYPE;
  completion memory.v5_local_inference_event%ROWTYPE;
  classification record;
  replayed memory.v5_local_inference_outcome_event%ROWTYPE;
  new_event_id uuid := gen_random_uuid();
  target_status text;
  next_result jsonb;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5 local record outcome requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_job_id IS NULL OR p_lease_token IS NULL
     OR p_completion_event_id IS NULL OR p_worker_id IS NULL
     OR btrim(p_worker_id)='' OR length(p_worker_id)>500
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_rejection_code !~ '^[a-z][a-z0-9_]{1,99}$' THEN
    RAISE EXCEPTION 'V5 local record outcome inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT * INTO classification
  FROM memory.classify_v5_local_inference_outcome_v2(p_rejection_code);
  IF classification.outcome_class<>'record_terminal'
     OR classification.disposition IS NULL THEN
    RAISE EXCEPTION 'V5 local outcome is not record-terminal'
      USING ERRCODE='23514';
  END IF;

  SELECT event.* INTO replayed
  FROM memory.v5_local_inference_outcome_event AS event
  WHERE event.owner_user_id=actor AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.job_id<>p_job_id
       OR replayed.completion_event_id<>p_completion_event_id
       OR replayed.rejection_code<>p_rejection_code
       OR replayed.disposition<>classification.disposition
       OR replayed.normalized_reason_code<>
          classification.normalized_reason_code THEN
      RAISE EXCEPTION 'V5 local record outcome replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      replayed.job_id,
      CASE WHEN replayed.disposition='review_required'
        THEN 'review_required'::text ELSE 'skipped'::text END,
      replayed.disposition,replayed.normalized_reason_code,
      replayed.event_id,'replayed'::text;
    RETURN;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_v5_local_record_outcome',actor::text,
      p_job_id::text),0
  ));

  SELECT job.* INTO current_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id
  FOR UPDATE;
  IF NOT FOUND OR current_job.status<>'processing'
     OR current_job.lease_token<>p_lease_token
     OR current_job.worker_id IS DISTINCT FROM p_worker_id
     OR current_job.lease_expires_at<=clock_timestamp()
     OR current_job.evidence_content_sha256
        IS DISTINCT FROM p_expected_content_sha256 THEN
    RAISE EXCEPTION 'V5 local record outcome lease or content is invalid'
      USING ERRCODE='23514';
  END IF;

  SELECT event.* INTO completion
  FROM memory.v5_local_inference_event AS event
  WHERE event.owner_user_id=actor
    AND event.event_id=p_completion_event_id
    AND event.job_id=p_job_id
    AND event.action='completed'
    AND event.outcome='rejected'
    AND event.rejection_code=p_rejection_code;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'V5 local record completion is absent or mismatched'
      USING ERRCODE='23514';
  END IF;

  target_status:=CASE WHEN classification.disposition='review_required'
    THEN 'review_required' ELSE 'skipped' END;
  next_result:=jsonb_set(
    current_job.result,
    '{final}',
    jsonb_build_object(
      'status',target_status,
      'payload',jsonb_build_object(
        'contract_version','memory_v1_v5_local_record_outcome_v2',
        'outcome_class','record_terminal',
        'disposition',classification.disposition,
        'reason_code',classification.normalized_reason_code,
        'policy_version','memory_v1_v5_local_outcome_policy_v2',
        'write_counts',jsonb_build_object(
          'packets',0,'claims',0,'qdrant',0,'prompt_influence',0
        )
      )
    ),
    true
  );
  IF pg_column_size(next_result)>32768 THEN
    RAISE EXCEPTION 'sanitized V5 local outcome exceeds queue budget'
      USING ERRCODE='22023';
  END IF;

  INSERT INTO memory.v5_local_inference_outcome_event(
    event_id,owner_user_id,operation_id,job_id,completion_event_id,
    rejection_code,outcome_class,disposition,normalized_reason_code,
    policy_version
  ) VALUES (
    new_event_id,actor,p_operation_id,p_job_id,p_completion_event_id,
    p_rejection_code,'record_terminal',classification.disposition,
    classification.normalized_reason_code,
    'memory_v1_v5_local_outcome_policy_v2'
  );

  UPDATE memory.evidence_extraction_job AS job
  SET status=target_status::memory.evidence_extraction_job_status,
      lease_token=NULL,lease_expires_at=NULL,last_error=NULL,
      result=next_result
  WHERE job.owner_user_id=actor AND job.job_id=p_job_id;

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,from_status,to_status,
    actor_type,actor_ref,details
  ) VALUES (
    actor,p_job_id,p_operation_id,target_status,'processing',
    target_status::memory.evidence_extraction_job_status,'worker',p_worker_id,
    jsonb_build_object(
      'outcome_event_id',new_event_id,
      'completion_event_id',p_completion_event_id,
      'outcome_class','record_terminal',
      'disposition',classification.disposition,
      'reason_code',classification.normalized_reason_code,
      'policy_version','memory_v1_v5_local_outcome_policy_v2'
    )
  );

  RETURN QUERY SELECT
    p_job_id,target_status,classification.disposition,
    classification.normalized_reason_code,new_event_id,'applied'::text;
END
$function$;

ALTER FUNCTION memory.finalize_owner_v5_local_record_outcome_v2(
  uuid,uuid,uuid,text,text,uuid,text
) OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.finalize_owner_v5_local_record_outcome_v2(
  uuid,uuid,uuid,text,text,uuid,text
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.finalize_owner_v5_local_record_outcome_v2(
  uuid,uuid,uuid,text,text,uuid,text
) TO brains_app;

CREATE OR REPLACE FUNCTION memory.owner_v5_local_inference_circuit_state_v2(
  p_provider_id text,
  p_provider_version text,
  p_provider_model_sha256 text,
  p_model_file_sha256 text,
  p_runtime_revision_sha256 text,
  p_policy_compiler_sha256 text,
  p_failure_threshold integer,
  p_cooldown_seconds integer DEFAULT 900
)
RETURNS TABLE(
  circuit_state text,
  systemic_failure_count integer,
  open_reason text,
  opened_at timestamptz,
  cooldown_until timestamptz,
  half_open_reservation_event_id uuid
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  reset_at timestamptz;
  reset_event_id uuid;
  latest_systemic_at timestamptz;
  latest_systemic_event_id uuid;
  latest_systemic_code text;
  failure_count integer := 0;
  immediate_failure boolean := false;
  half_open_event uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5 local circuit status requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_provider_id<>'local_llama_cpp'
     OR p_provider_version !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
     OR p_provider_model_sha256 !~ '^[0-9a-f]{64}$'
     OR p_model_file_sha256 !~ '^[0-9a-f]{64}$'
     OR p_runtime_revision_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_compiler_sha256 !~ '^[0-9a-f]{64}$'
     OR p_failure_threshold NOT BETWEEN 1 AND 10
     OR p_cooldown_seconds NOT BETWEEN 60 AND 86400 THEN
    RAISE EXCEPTION 'V5 local circuit status inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT completed.created_at,completed.event_id
  INTO reset_at,reset_event_id
  FROM memory.v5_local_inference_event AS completed
  LEFT JOIN LATERAL memory.classify_v5_local_inference_outcome_v2(
    completed.rejection_code
  ) AS classification ON completed.rejection_code IS NOT NULL
  WHERE completed.owner_user_id=actor
    AND completed.action='completed'
    AND completed.provider_id=p_provider_id
    AND completed.provider_version=p_provider_version
    AND completed.provider_model_sha256=p_provider_model_sha256
    AND completed.model_file_sha256=p_model_file_sha256
    AND completed.runtime_revision_sha256=p_runtime_revision_sha256
    AND completed.policy_compiler_sha256=p_policy_compiler_sha256
    AND (
      completed.outcome='accepted'
      OR (
        classification.outcome_class='record_terminal'
        AND completed.local_model_calls>0
      )
    )
  ORDER BY completed.created_at DESC,completed.event_id DESC
  LIMIT 1;

  SELECT
    count(*)::integer,
    coalesce(bool_or(classification.immediate_open),false),
    (array_agg(
      completed.rejection_code
      ORDER BY completed.created_at DESC,completed.event_id DESC
    ))[1],
    max(completed.created_at),
    (array_agg(
      completed.event_id
      ORDER BY completed.created_at DESC,completed.event_id DESC
    ))[1]
  INTO
    failure_count,
    immediate_failure,
    latest_systemic_code,
    latest_systemic_at,
    latest_systemic_event_id
  FROM memory.v5_local_inference_event AS completed
  CROSS JOIN LATERAL memory.classify_v5_local_inference_outcome_v2(
    completed.rejection_code
  ) AS classification
  WHERE completed.owner_user_id=actor
    AND completed.action='completed'
    AND completed.provider_id=p_provider_id
    AND completed.provider_version=p_provider_version
    AND completed.provider_model_sha256=p_provider_model_sha256
    AND completed.model_file_sha256=p_model_file_sha256
    AND completed.runtime_revision_sha256=p_runtime_revision_sha256
    AND completed.policy_compiler_sha256=p_policy_compiler_sha256
    AND classification.circuit_impact
    AND (
      reset_at IS NULL
      OR (completed.created_at,completed.event_id)>
         (reset_at,reset_event_id)
    );

  IF NOT immediate_failure AND failure_count<p_failure_threshold THEN
    RETURN QUERY SELECT
      'closed'::text,failure_count,NULL::text,NULL::timestamptz,
      NULL::timestamptz,NULL::uuid;
    RETURN;
  END IF;

  opened_at:=latest_systemic_at;
  open_reason:=coalesce(latest_systemic_code,'unclassified_failure');
  IF immediate_failure THEN
    RETURN QUERY SELECT
      'open'::text,failure_count,open_reason,opened_at,
      NULL::timestamptz,NULL::uuid;
    RETURN;
  END IF;

  cooldown_until:=opened_at+make_interval(secs=>p_cooldown_seconds);
  IF clock_timestamp()<cooldown_until THEN
    RETURN QUERY SELECT
      'open'::text,failure_count,open_reason,opened_at,
      cooldown_until,NULL::uuid;
    RETURN;
  END IF;

  SELECT reservation.event_id
  INTO half_open_event
  FROM memory.v5_local_inference_event AS reservation
  WHERE reservation.owner_user_id=actor
    AND reservation.action='reserved'
    AND reservation.provider_id=p_provider_id
    AND reservation.provider_version=p_provider_version
    AND reservation.provider_model_sha256=p_provider_model_sha256
    AND reservation.model_file_sha256=p_model_file_sha256
    AND reservation.runtime_revision_sha256=p_runtime_revision_sha256
    AND reservation.policy_compiler_sha256=p_policy_compiler_sha256
    AND reservation.created_at>=cooldown_until
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_inference_event AS completion
      WHERE completion.owner_user_id=actor
        AND completion.action='completed'
        AND completion.reservation_event_id=reservation.event_id
    )
  ORDER BY reservation.created_at DESC,reservation.event_id DESC
  LIMIT 1;

  IF half_open_event IS NOT NULL THEN
    RETURN QUERY SELECT
      'half_open_inflight'::text,failure_count,open_reason,opened_at,
      cooldown_until,half_open_event;
    RETURN;
  END IF;
  RETURN QUERY SELECT
    'half_open_ready'::text,failure_count,open_reason,opened_at,
    cooldown_until,NULL::uuid;
END
$function$;

ALTER FUNCTION memory.owner_v5_local_inference_circuit_state_v2(
  text,text,text,text,text,text,integer,integer
) OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.owner_v5_local_inference_circuit_state_v2(
  text,text,text,text,text,text,integer,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.owner_v5_local_inference_circuit_state_v2(
  text,text,text,text,text,text,integer,integer
) TO brains_app;

DO $migration$
DECLARE
  function_oid constant regprocedure :=
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure;
  expected_before constant text :=
    '62175d9205544eaae20521dbb819d9c8c0838fbb94287c8abacdaed61ab93160';
  old_declarations constant text :=
    '  latest_count integer;' || E'\n' ||
    '  latest_all_rejected boolean;' || E'\n' ||
    '  rejection_count integer;' || E'\n' ||
    '  block_reason text;';
  new_declarations constant text :=
    '  rejection_count integer;' || E'\n' ||
    '  block_reason text;' || E'\n' ||
    '  circuit record;';
  old_control constant text :=
    '  SELECT count(*)::integer,coalesce(bool_and(recent.outcome=''rejected''),false)' || E'\n' ||
    '  INTO latest_count,latest_all_rejected' || E'\n' ||
    '  FROM (' || E'\n' ||
    '    SELECT event.outcome' || E'\n' ||
    '    FROM memory.v5_local_inference_event AS event' || E'\n' ||
    '    WHERE event.owner_user_id=actor AND event.action=''completed''' || E'\n' ||
    '      AND event.provider_id=p_provider_id' || E'\n' ||
    '      AND event.provider_version=p_provider_version' || E'\n' ||
    '      AND event.provider_model_sha256=p_provider_model_sha256' || E'\n' ||
    '      AND event.model_file_sha256=p_model_file_sha256' || E'\n' ||
    '      AND event.runtime_revision_sha256=p_runtime_revision_sha256' || E'\n' ||
    '      AND event.policy_compiler_sha256=p_policy_compiler_sha256' || E'\n' ||
    '      AND event.rejection_code IS DISTINCT FROM ''local_worker_abandoned''' || E'\n' ||
    '    ORDER BY event.created_at DESC,event.event_id DESC' || E'\n' ||
    '    LIMIT p_failure_threshold' || E'\n' ||
    '  ) AS recent;' || E'\n' ||
    '  rejection_count := CASE' || E'\n' ||
    '    WHEN latest_count=p_failure_threshold AND latest_all_rejected' || E'\n' ||
    '      THEN p_failure_threshold ELSE 0' || E'\n' ||
    '  END;' || E'\n' ||
    '  IF reserved_count>=p_max_reserved_jobs THEN' || E'\n' ||
    '    block_reason := ''quota_exhausted'';' || E'\n' ||
    '  ELSIF rejection_count>=p_failure_threshold THEN' || E'\n' ||
    '    block_reason := ''circuit_open'';' || E'\n' ||
    '  END IF;';
  new_control constant text :=
    '  SELECT * INTO circuit' || E'\n' ||
    '  FROM memory.owner_v5_local_inference_circuit_state_v2(' || E'\n' ||
    '    p_provider_id,p_provider_version,p_provider_model_sha256,' || E'\n' ||
    '    p_model_file_sha256,p_runtime_revision_sha256,' || E'\n' ||
    '    p_policy_compiler_sha256,p_failure_threshold,900' || E'\n' ||
    '  );' || E'\n' ||
    '  rejection_count:=least(circuit.systemic_failure_count,10);' || E'\n' ||
    '  IF circuit.circuit_state IN (''open'',''half_open_inflight'') THEN' || E'\n' ||
    '    block_reason := ''circuit_open'';' || E'\n' ||
    '  ELSIF reserved_count>=p_max_reserved_jobs THEN' || E'\n' ||
    '    block_reason := ''quota_exhausted'';' || E'\n' ||
    '  END IF;';
  source text;
  source_sha text;
BEGIN
  SELECT pg_get_functiondef(function_oid) INTO source;
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>expected_before THEN
    RAISE EXCEPTION 'local outcome circuit baseline changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  IF (length(source)-length(replace(source,old_declarations,'')))
       / length(old_declarations)<>1
     OR (length(source)-length(replace(source,old_control,'')))
       / length(old_control)<>1 THEN
    RAISE EXCEPTION 'local outcome circuit replacement anchor changed'
      USING ERRCODE='23514';
  END IF;
  source:=replace(source,old_declarations,new_declarations);
  source:=replace(source,old_control,new_control);
  EXECUTE source;
END
$migration$;

ALTER FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) TO brains_app;

CREATE OR REPLACE FUNCTION memory.owner_v5_local_inference_status_v1(
  p_max_attempts integer,
  p_provider_id text,
  p_provider_version text,
  p_provider_model_sha256 text,
  p_model_file_sha256 text,
  p_runtime_revision_sha256 text,
  p_policy_compiler_sha256 text,
  p_rolling_window_seconds integer,
  p_max_reserved_jobs integer,
  p_failure_threshold integer,
  p_cooldown_seconds integer DEFAULT 900
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  circuit record;
  eligible_backlog integer;
  active_leases integer;
  reserved_in_window integer;
  last_dispatch_at timestamptz;
  last_terminal record;
  completed_1h integer;
  completed_24h integer;
  accepted_24h integer;
  record_terminal_24h integer;
  systemic_24h integer;
  blocked_wakeups integer;
  database_state text;
  activity text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5 local status requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_max_attempts NOT BETWEEN 1 AND 4
     OR p_rolling_window_seconds NOT BETWEEN 3600 AND 604800
     OR p_max_reserved_jobs NOT BETWEEN 1 AND 100 THEN
    RAISE EXCEPTION 'V5 local status inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT * INTO circuit
  FROM memory.owner_v5_local_inference_circuit_state_v2(
    p_provider_id,p_provider_version,p_provider_model_sha256,
    p_model_file_sha256,p_runtime_revision_sha256,
    p_policy_compiler_sha256,p_failure_threshold,p_cooldown_seconds
  );

  SELECT
    count(*) FILTER (
      WHERE job.status IN ('pending','error')
        AND job.attempts<p_max_attempts
        AND job.available_at<=clock_timestamp()
    )::integer,
    count(*) FILTER (
      WHERE job.status='processing'
        AND job.lease_expires_at>clock_timestamp()
    )::integer
  INTO eligible_backlog,active_leases
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor
    AND job.route='relational_extraction';

  SELECT
    count(*)::integer,
    max(event.created_at)
  INTO reserved_in_window,last_dispatch_at
  FROM memory.v5_local_inference_event AS event
  WHERE event.owner_user_id=actor
    AND event.action='reserved'
    AND event.provider_id=p_provider_id
    AND event.provider_version=p_provider_version
    AND event.provider_model_sha256=p_provider_model_sha256
    AND event.model_file_sha256=p_model_file_sha256
    AND event.runtime_revision_sha256=p_runtime_revision_sha256
    AND event.policy_compiler_sha256=p_policy_compiler_sha256
    AND event.created_at>=
      clock_timestamp()-make_interval(secs=>p_rolling_window_seconds);

  SELECT
    completed.created_at AS terminal_at,
    completed.outcome AS terminal_outcome,
    classification.outcome_class,
    classification.normalized_reason_code
  INTO last_terminal
  FROM memory.v5_local_inference_event AS completed
  LEFT JOIN LATERAL memory.classify_v5_local_inference_outcome_v2(
    completed.rejection_code
  ) AS classification ON completed.rejection_code IS NOT NULL
  WHERE completed.owner_user_id=actor
    AND completed.action='completed'
    AND completed.provider_id=p_provider_id
    AND completed.provider_version=p_provider_version
    AND completed.provider_model_sha256=p_provider_model_sha256
    AND completed.model_file_sha256=p_model_file_sha256
    AND completed.runtime_revision_sha256=p_runtime_revision_sha256
    AND completed.policy_compiler_sha256=p_policy_compiler_sha256
  ORDER BY completed.created_at DESC,completed.event_id DESC
  LIMIT 1;

  SELECT
    count(*) FILTER (
      WHERE completed.created_at>=clock_timestamp()-interval '1 hour'
    )::integer,
    count(*)::integer,
    count(*) FILTER (WHERE completed.outcome='accepted')::integer,
    count(*) FILTER (
      WHERE classification.outcome_class='record_terminal'
    )::integer,
    count(*) FILTER (
      WHERE classification.circuit_impact
    )::integer
  INTO completed_1h,completed_24h,accepted_24h,record_terminal_24h,systemic_24h
  FROM memory.v5_local_inference_event AS completed
  LEFT JOIN LATERAL memory.classify_v5_local_inference_outcome_v2(
    completed.rejection_code
  ) AS classification ON completed.rejection_code IS NOT NULL
  WHERE completed.owner_user_id=actor
    AND completed.action='completed'
    AND completed.provider_id=p_provider_id
    AND completed.provider_version=p_provider_version
    AND completed.provider_model_sha256=p_provider_model_sha256
    AND completed.model_file_sha256=p_model_file_sha256
    AND completed.runtime_revision_sha256=p_runtime_revision_sha256
    AND completed.policy_compiler_sha256=p_policy_compiler_sha256
    AND completed.created_at>=clock_timestamp()-interval '24 hours';

  SELECT count(*)::integer INTO blocked_wakeups
  FROM memory.v5_local_inference_event AS blocked
  WHERE blocked.owner_user_id=actor
    AND blocked.action='blocked'
    AND blocked.outcome='circuit_open'
    AND blocked.created_at>=coalesce(
      circuit.opened_at,
      clock_timestamp()-interval '24 hours'
    );

  database_state:=CASE
    WHEN circuit.circuit_state IN ('open','half_open_inflight')
      THEN 'circuit_blocked'
    WHEN reserved_in_window>=p_max_reserved_jobs
      THEN 'quota_limited'
    WHEN eligible_backlog=0 AND active_leases=0
      THEN 'idle_no_eligible_work'
    ELSE 'running'
  END;
  activity:=CASE
    WHEN database_state IN ('circuit_blocked','quota_limited')
      THEN 'blocked'
    WHEN active_leases>0
      THEN 'active'
    WHEN database_state='running'
      THEN 'dispatch_ready'
    ELSE 'none'
  END;

  RETURN jsonb_build_object(
    'contract_version','memory_v1_v5_local_status_v1',
    'observed_at',clock_timestamp(),
    'database_state',database_state,
    'activity',activity,
    'eligible_backlog',eligible_backlog,
    'active_leases',active_leases,
    'last_successful_dispatch_at',last_dispatch_at,
    'last_terminal_outcome',CASE
      WHEN last_terminal.terminal_at IS NULL THEN NULL
      ELSE jsonb_build_object(
        'at',last_terminal.terminal_at,
        'outcome',last_terminal.terminal_outcome,
        'outcome_class',coalesce(last_terminal.outcome_class,'success'),
        'reason_code',last_terminal.normalized_reason_code
      )
    END,
    'circuit',jsonb_build_object(
      'state',circuit.circuit_state,
      'systemic_failure_count',circuit.systemic_failure_count,
      'open_reason',circuit.open_reason,
      'opened_at',circuit.opened_at,
      'cooldown_until',circuit.cooldown_until,
      'blocked_wakeup_count',blocked_wakeups
    ),
    'quota',jsonb_build_object(
      'rolling_window_seconds',p_rolling_window_seconds,
      'max_reserved_jobs',p_max_reserved_jobs,
      'reserved_jobs',reserved_in_window
    ),
    'throughput',jsonb_build_object(
      'completed_1h',completed_1h,
      'completed_24h',completed_24h,
      'accepted_24h',accepted_24h,
      'record_terminal_24h',record_terminal_24h,
      'systemic_failure_24h',systemic_24h
    )
  );
END
$function$;

ALTER FUNCTION memory.owner_v5_local_inference_status_v1(
  integer,text,text,text,text,text,text,integer,integer,integer,integer
) OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.owner_v5_local_inference_status_v1(
  integer,text,text,text,text,text,text,integer,integer,integer,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.owner_v5_local_inference_status_v1(
  integer,text,text,text,text,text,text,integer,integer,integer,integer
) TO brains_app;

COMMIT;
