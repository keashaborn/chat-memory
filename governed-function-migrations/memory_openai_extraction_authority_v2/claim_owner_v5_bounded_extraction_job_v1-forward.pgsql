CREATE OR REPLACE FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(p_operation_id uuid, p_run_id uuid, p_route text, p_worker_id text, p_lease_seconds integer, p_max_attempts integer, p_provider_id text, p_provider_version text, p_provider_model_sha256 text, p_rolling_window_seconds integer, p_max_reserved_calls integer, p_failure_threshold integer)
RETURNS TABLE(
  job_id uuid,
  evidence_id uuid,
  lease_token uuid,
  status text,
  route text,
  attempts integer,
  evidence_content_sha256 text,
  evidence_kind text,
  evidence_source_system text,
  evidence_external_id text,
  evidence_content text,
  evidence_observed_at timestamptz,
  evidence_recorded_at timestamptz,
  evidence_sensitivity text,
  checkpoint_sequence integer,
  checkpoint_sha256 text,
  result jsonb,
  reservation_event_id uuid,
  control_outcome text,
  reserved_calls_in_window integer,
  consecutive_rejections integer,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
<<memory_openai_extraction_authority_v2>>
DECLARE
  actor uuid;
  worker_sha text;
  new_lease_token uuid := gen_random_uuid();
  new_reservation_id uuid := gen_random_uuid();
  replayed memory.v5_extraction_call_event%ROWTYPE;
  claimed record;
  reserved_count integer;
  latest_completion_count integer;
  latest_all_rejected boolean;
  rejection_count integer;
  block_reason text;
  exact_mode text := nullif(
    current_setting('memory.v5_exact_claim_mode',true),''
  );
  exact_job_text text := nullif(
    current_setting('memory.v5_exact_job_id',true),''
  );
  exact_content_sha text := nullif(
    current_setting('memory.v5_exact_content_sha256',true),''
  );
  exact_job uuid;
  exact_target boolean := false;
  prefilter_mode text := nullif(
    current_setting('memory.openai_prefilter_mode',true),''
  );
  reservation_mode text := nullif(
    current_setting('memory.openai_reservation_mode',true),''
  );
  finalize_mode text := nullif(
    current_setting('memory.openai_finalize_mode',true),''
  );
  request_receipt_text text := nullif(
    current_setting('memory.openai_request_receipt_v1',true),''
  );
  prefilter_receipt_text text := nullif(
    current_setting('memory.openai_prefilter_receipt_v1',true),''
  );
  completion_audit_text text := nullif(
    current_setting('memory.openai_completion_audit_v1',true),''
  );
  request_receipt jsonb;
  prefilter_receipt jsonb;
  completion_audit_value jsonb;
  request_receipt_sha text;
  completion_audit_sha text;
  target_job memory.evidence_extraction_job%ROWTYPE;
  prior_status memory.evidence_extraction_job_status;
  request_row memory.openai_provider_request_receipt_v1%ROWTYPE;
  completion_receipt_row memory.openai_provider_completion_receipt_v1%ROWTYPE;
  completion_row memory.v5_extraction_call_event%ROWTYPE;
  completion_receipt_id uuid;
  completion_was_inserted boolean := false;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'bounded V5 extraction claim requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF exact_mode IS NOT NULL
     OR exact_job_text IS NOT NULL
     OR exact_content_sha IS NOT NULL THEN
    IF exact_mode IS DISTINCT FROM 'memory_v1_openai_exact_job_claim_v1'
       OR exact_job_text IS NULL
       OR exact_job_text !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR exact_content_sha IS NULL
       OR exact_content_sha !~ '^[0-9a-f]{64}$' THEN
      RAISE EXCEPTION 'exact V5 extraction target is invalid'
        USING ERRCODE='22023';
    END IF;
    exact_job := exact_job_text::uuid;
    exact_target := true;
  END IF;

  IF p_operation_id IS NULL
     OR p_run_id IS NULL
     OR p_route<>'relational_extraction'
     OR p_worker_id IS NULL
     OR btrim(p_worker_id)=''
     OR length(p_worker_id)>500
     OR p_lease_seconds NOT BETWEEN 30 AND 3600
     OR p_max_attempts NOT BETWEEN 1 AND 3
     OR p_provider_id<>'openai_responses'
     OR p_provider_version IS NULL
     OR p_provider_version !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
     OR p_provider_model_sha256 !~ '^[0-9a-f]{64}$'
     OR p_rolling_window_seconds NOT BETWEEN 3600 AND 604800
     OR p_max_reserved_calls NOT BETWEEN 1 AND 100
     OR p_failure_threshold NOT BETWEEN 1 AND 10 THEN
    RAISE EXCEPTION 'bounded V5 extraction claim inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  IF num_nonnulls(prefilter_mode,reservation_mode,finalize_mode)>1
     OR (prefilter_mode IS NOT NULL AND prefilter_mode NOT IN (
       'memory_v1_openai_prefilter_probe_v1',
       'memory_v1_openai_prefilter_skip_v1'
     ))
     OR (reservation_mode IS NOT NULL
       AND reservation_mode<>'memory_v1_openai_request_reservation_v1')
     OR (finalize_mode IS NOT NULL
       AND finalize_mode<>'memory_v1_openai_request_receipt_finalize_v1')
     OR (
       (prefilter_mode IS NOT NULL
        OR reservation_mode IS NOT NULL
        OR finalize_mode IS NOT NULL)
       AND NOT exact_target
     ) THEN
    RAISE EXCEPTION 'OpenAI governed extraction mode is invalid'
      USING ERRCODE='22023';
  END IF;

  worker_sha := encode(
    public.digest(convert_to(
      CASE WHEN exact_target THEN concat_ws('|',p_worker_id,
        'memory_v1_openai_exact_job_claim_v1',exact_job::text,
        exact_content_sha)
      ELSE p_worker_id END,
      'UTF8'),'sha256'),'hex'
  );
  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_v5_extraction',actor::text,p_route),0
  ));

  IF prefilter_mode='memory_v1_openai_prefilter_probe_v1' THEN
    SELECT extraction_job.* INTO target_job
    FROM memory.evidence_extraction_job AS extraction_job
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=extraction_job.owner_user_id
     AND evidence.evidence_id=extraction_job.evidence_id
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.job_id=exact_job
      AND extraction_job.route=p_route
      AND extraction_job.evidence_content_sha256=exact_content_sha
      AND extraction_job.available_at<=clock_timestamp()
      AND extraction_job.attempts<p_max_attempts
      AND (
        extraction_job.status IN ('pending','error')
        OR (
          extraction_job.status='processing'
          AND extraction_job.lease_expires_at<=clock_timestamp()
        )
      )
      AND evidence.content IS NOT NULL
      AND evidence.content_sha256=exact_content_sha;
    IF NOT FOUND THEN
      RETURN;
    END IF;
    RETURN QUERY
    SELECT target_job.job_id,target_job.evidence_id,NULL::uuid,
      target_job.status::text,target_job.route,target_job.attempts,
      target_job.evidence_content_sha256,evidence.kind::text,
      evidence.source_system,evidence.external_id,evidence.content,
      evidence.observed_at,evidence.recorded_at,evidence.sensitivity::text,
      target_job.checkpoint_sequence,target_job.checkpoint_sha256,
      target_job.result,NULL::uuid,'prefilter_probe'::text,0,0,
      'read_only'::text
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id=actor
      AND evidence.evidence_id=target_job.evidence_id;
    RETURN;
  END IF;

  IF prefilter_mode='memory_v1_openai_prefilter_skip_v1' THEN
    IF prefilter_receipt_text IS NULL THEN
      RAISE EXCEPTION 'prefilter skip receipt is required'
        USING ERRCODE='22023';
    END IF;
    prefilter_receipt := prefilter_receipt_text::jsonb;
    IF jsonb_typeof(prefilter_receipt)<>'object'
       OR pg_column_size(prefilter_receipt)>4096
       OR prefilter_receipt->>'contract_version'
          <>'memory_v1_prefilter_skip_receipt_v1'
       OR (prefilter_receipt->>'external_model_calls')::integer<>0
       OR prefilter_receipt->>'gate_sha256' !~ '^[0-9a-f]{64}$'
       OR prefilter_receipt->>'reason_code'
          !~ '^[a-z][a-z0-9_]{1,99}$' THEN
      RAISE EXCEPTION 'prefilter skip receipt is invalid'
        USING ERRCODE='22023';
    END IF;
    request_receipt_sha := encode(public.digest(
      convert_to(prefilter_receipt::text,'UTF8'),'sha256'
    ),'hex');
    IF EXISTS (
      SELECT 1 FROM memory.evidence_extraction_event AS event
      WHERE event.owner_user_id=actor
        AND event.operation_id=p_operation_id
        AND event.job_id=exact_job
        AND event.event_type='skipped'
        AND event.details->>'result_sha256'=request_receipt_sha
    ) THEN
      SELECT extraction_job.* INTO target_job
      FROM memory.evidence_extraction_job AS extraction_job
      WHERE extraction_job.owner_user_id=actor
        AND extraction_job.job_id=exact_job;
      RETURN QUERY
      SELECT target_job.job_id,target_job.evidence_id,NULL::uuid,
        target_job.status::text,target_job.route,target_job.attempts,
        target_job.evidence_content_sha256,evidence.kind::text,
        evidence.source_system,evidence.external_id,evidence.content,
        evidence.observed_at,evidence.recorded_at,evidence.sensitivity::text,
        target_job.checkpoint_sequence,target_job.checkpoint_sha256,
        target_job.result,NULL::uuid,'prefilter_skipped'::text,0,0,
        'replayed'::text
      FROM memory.evidence AS evidence
      WHERE evidence.owner_user_id=actor
        AND evidence.evidence_id=target_job.evidence_id;
      RETURN;
    END IF;
    SELECT extraction_job.* INTO target_job
    FROM memory.evidence_extraction_job AS extraction_job
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.job_id=exact_job
      AND extraction_job.route=p_route
      AND extraction_job.evidence_content_sha256=exact_content_sha
      AND extraction_job.available_at<=clock_timestamp()
      AND extraction_job.attempts<p_max_attempts
      AND extraction_job.status IN ('pending','error')
    ;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'prefilter skip target is absent or unavailable'
        USING ERRCODE='23514';
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM memory.evidence AS evidence
      WHERE evidence.owner_user_id=actor
        AND evidence.evidence_id=target_job.evidence_id
        AND evidence.content IS NOT NULL
        AND evidence.content_sha256=exact_content_sha
    ) THEN
      RAISE EXCEPTION 'prefilter skip evidence binding is invalid'
        USING ERRCODE='23514';
    END IF;
    prior_status := target_job.status;
    UPDATE memory.evidence_extraction_job AS extraction_job
    SET status='skipped',lease_token=NULL,lease_expires_at=NULL,
      worker_id=p_worker_id,last_error=NULL,
      result=jsonb_set(
        extraction_job.result,'{final}',
        jsonb_build_object(
          'status','skipped','sha256',request_receipt_sha,
          'payload',prefilter_receipt
        ),true
      )
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.job_id=exact_job
    RETURNING extraction_job.* INTO target_job;
    IF pg_column_size(target_job.result)>32768 THEN
      RAISE EXCEPTION 'prefilter skip result exceeds job budget'
        USING ERRCODE='22023';
    END IF;
    INSERT INTO memory.evidence_extraction_event(
      owner_user_id,job_id,operation_id,event_type,from_status,to_status,
      actor_type,actor_ref,details
    ) VALUES (
      actor,exact_job,p_operation_id,'skipped',prior_status,'skipped',
      'worker',p_worker_id,
      jsonb_build_object(
        'result_sha256',request_receipt_sha,
        'reason_code',prefilter_receipt->>'reason_code',
        'gate_sha256',prefilter_receipt->>'gate_sha256',
        'external_model_calls',0
      )
    );
    RETURN QUERY
    SELECT target_job.job_id,target_job.evidence_id,NULL::uuid,
      target_job.status::text,target_job.route,target_job.attempts,
      target_job.evidence_content_sha256,evidence.kind::text,
      evidence.source_system,evidence.external_id,evidence.content,
      evidence.observed_at,evidence.recorded_at,evidence.sensitivity::text,
      target_job.checkpoint_sequence,target_job.checkpoint_sha256,
      target_job.result,NULL::uuid,'prefilter_skipped'::text,0,0,
      'applied'::text
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id=actor
      AND evidence.evidence_id=target_job.evidence_id;
    RETURN;
  END IF;

  IF finalize_mode='memory_v1_openai_request_receipt_finalize_v1' THEN
    IF completion_audit_text IS NULL THEN
      RAISE EXCEPTION 'provider completion audit is required'
        USING ERRCODE='22023';
    END IF;
    completion_audit_value := completion_audit_text::jsonb;
    IF jsonb_typeof(completion_audit_value)<>'object'
       OR pg_column_size(completion_audit_value)>24576
       OR completion_audit_value->>'contract_version'
          <>'memory_v1_openai_provider_completion_audit_v1'
       OR completion_audit_value->>'outcome' NOT IN ('accepted','rejected')
       OR completion_audit_value->>'request_sha256' !~ '^[0-9a-f]{64}$'
       OR completion_audit_value->>'reservation_event_id'
          !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR (completion_audit_value->>'reservation_event_id')::uuid IS NULL THEN
      RAISE EXCEPTION 'provider completion audit is invalid'
        USING ERRCODE='22023';
    END IF;
    completion_receipt_id :=
      (completion_audit_value->>'reservation_event_id')::uuid;
    completion_audit_sha := encode(public.digest(
      convert_to(completion_audit_value::text,'UTF8'),'sha256'
    ),'hex');
    SELECT receipt.* INTO request_row
    FROM memory.openai_provider_request_receipt_v1 AS receipt
    WHERE receipt.owner_user_id=actor
      AND receipt.receipt_id=completion_receipt_id
      AND receipt.run_id=p_run_id
      AND receipt.job_id=exact_job
      AND receipt.request_sha256=completion_audit_value->>'request_sha256'
      AND receipt.provider_id=p_provider_id
      AND receipt.provider_version=p_provider_version
      AND receipt.provider_model_sha256=p_provider_model_sha256
      AND receipt.worker_id_sha256=worker_sha;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'provider request receipt is absent or mismatched'
        USING ERRCODE='23514';
    END IF;
    SELECT event.* INTO completion_row
    FROM memory.v5_extraction_call_event AS event
    WHERE event.owner_user_id=actor
      AND event.reservation_event_id=completion_receipt_id
      AND event.action='completed'
      AND event.run_id=p_run_id
      AND event.job_id=exact_job
      AND event.outcome=completion_audit_value->>'outcome';
    IF NOT FOUND THEN
      RAISE EXCEPTION 'provider completion event is absent or mismatched'
        USING ERRCODE='23514';
    END IF;
    SELECT receipt.* INTO completion_receipt_row
    FROM memory.openai_provider_completion_receipt_v1 AS receipt
    WHERE receipt.owner_user_id=actor
      AND receipt.receipt_id=completion_receipt_id;
    IF FOUND THEN
      IF completion_receipt_row.operation_id<>p_operation_id
         OR completion_receipt_row.run_id<>p_run_id
         OR completion_receipt_row.job_id<>exact_job
         OR completion_receipt_row.outcome<>completion_audit_value->>'outcome'
         OR completion_receipt_row.completion_audit_sha256<>completion_audit_sha
         OR completion_receipt_row.completion_audit<>completion_audit_value THEN
        RAISE EXCEPTION 'provider request receipt finalization replay conflicts'
          USING ERRCODE='23514';
      END IF;
    ELSE
      INSERT INTO memory.openai_provider_completion_receipt_v1(
        receipt_id,owner_user_id,operation_id,run_id,job_id,outcome,
        completion_audit_sha256,completion_audit
      ) VALUES (
        completion_receipt_id,actor,p_operation_id,p_run_id,exact_job,
        completion_audit_value->>'outcome',completion_audit_sha,
        completion_audit_value
      ) RETURNING * INTO completion_receipt_row;
      completion_was_inserted := true;
    END IF;
    SELECT extraction_job.* INTO target_job
    FROM memory.evidence_extraction_job AS extraction_job
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.job_id=exact_job
      AND extraction_job.evidence_content_sha256=exact_content_sha;
    RETURN QUERY
    SELECT target_job.job_id,target_job.evidence_id,target_job.lease_token,
      target_job.status::text,target_job.route,target_job.attempts,
      target_job.evidence_content_sha256,evidence.kind::text,
      evidence.source_system,evidence.external_id,evidence.content,
      evidence.observed_at,evidence.recorded_at,evidence.sensitivity::text,
      target_job.checkpoint_sequence,target_job.checkpoint_sha256,
      target_job.result,completion_receipt_id,'receipt_finalized'::text,
      0,0,CASE WHEN completion_was_inserted
        THEN 'applied' ELSE 'replayed' END
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id=actor
      AND evidence.evidence_id=target_job.evidence_id;
    RETURN;
  END IF;

  IF reservation_mode='memory_v1_openai_request_reservation_v1' THEN
    IF request_receipt_text IS NULL THEN
      RAISE EXCEPTION 'provider request receipt is required'
        USING ERRCODE='22023';
    END IF;
    request_receipt := request_receipt_text::jsonb;
    IF jsonb_typeof(request_receipt)<>'object'
       OR pg_column_size(request_receipt)>16384
       OR request_receipt->>'contract_version'
          <>'memory_v1_openai_provider_request_receipt_v1'
       OR request_receipt->>'job_id'<>exact_job::text
       OR request_receipt->>'evidence_content_sha256'<>exact_content_sha
       OR request_receipt->>'run_id'<>p_run_id::text
       OR request_receipt->>'worker_id_sha256'<>worker_sha
       OR request_receipt->>'model' IS NULL
       OR encode(public.digest(
         convert_to(request_receipt->>'model','UTF8'),'sha256'
       ),'hex')<>p_provider_model_sha256
       OR request_receipt->>'request_sha256' !~ '^[0-9a-f]{64}$'
       OR request_receipt->>'request_id_sha256' !~ '^[0-9a-f]{64}$'
       OR request_receipt->>'source_sha256'<>exact_content_sha
       OR request_receipt->>'selected_input_sha256' !~ '^[0-9a-f]{64}$'
       OR request_receipt->>'output_schema_sha256' !~ '^[0-9a-f]{64}$'
       OR request_receipt->>'task_contract_sha256' !~ '^[0-9a-f]{64}$'
       OR request_receipt->>'model_policy_sha256' !~ '^[0-9a-f]{64}$'
       OR request_receipt->>'gate_policy_sha256' !~ '^[0-9a-f]{64}$'
       OR request_receipt->>'privacy_policy_sha256' !~ '^[0-9a-f]{64}$'
       OR request_receipt->>'privacy_authorization_sha256'
          !~ '^[0-9a-f]{64}$'
       OR request_receipt->>'budget_policy_sha256' !~ '^[0-9a-f]{64}$'
       OR request_receipt->>'pricing_policy_sha256' !~ '^[0-9a-f]{64}$'
       OR (request_receipt->>'max_attempts')::integer<>1
       OR (request_receipt->>'max_output_tokens')::integer NOT BETWEEN 16 AND 4096
       OR (request_receipt->>'estimated_input_tokens')::integer<=0
       OR (request_receipt->>'timeout_milliseconds')::integer NOT BETWEEN 1000 AND 180000
       OR (request_receipt->>'maximum_cost_microusd')::bigint<=0
       OR (request_receipt->>'max_request_microusd')::bigint
          <(request_receipt->>'maximum_cost_microusd')::bigint
       OR (request_receipt->>'max_utc_day_microusd')::bigint<=0
       OR jsonb_typeof(request_receipt->'pricing_rates')<>'object'
       OR EXISTS (
         SELECT 1 FROM jsonb_object_keys(request_receipt) AS keys(key_name)
         WHERE key_name<>ALL(ARRAY[
           'budget_policy_sha256','budget_policy_version',
           'contract_version','estimated_input_tokens',
           'evidence_content_sha256','gate_policy_sha256',
           'instructions_sha256','job_id','max_attempts',
           'max_output_tokens','max_request_microusd',
           'max_utc_day_microusd','maximum_cost_microusd','model',
           'model_policy_sha256','output_schema_sha256',
           'owner_binding_sha256','pipeline_version','pricing_policy_sha256',
           'pricing_policy_version','pricing_rates',
           'privacy_authorization_sha256','privacy_policy_sha256',
           'privacy_policy_version','purpose','request_id_sha256',
           'request_sha256','retention_attestation_sha256',
           'retention_mode','run_id','safety_identifier_sha256',
           'selected_input_sha256','source_sha256',
           'standard_retention_risk_accepted','task_contract_sha256',
           'timeout_milliseconds','worker_id_sha256'
         ]::text[])
       )
       OR (SELECT count(*) FROM jsonb_object_keys(request_receipt))<>37
       OR EXISTS (
         SELECT 1 FROM jsonb_object_keys(request_receipt->'pricing_rates')
           AS rates(rate_name)
         WHERE rate_name<>ALL(ARRAY[
           'input_microusd_per_million_tokens',
           'cached_input_microusd_per_million_tokens',
           'cache_write_input_microusd_per_million_tokens',
           'output_microusd_per_million_tokens'
         ]::text[])
       )
       OR (SELECT count(*) FROM jsonb_object_keys(
         request_receipt->'pricing_rates'
       ))<>4 THEN
      RAISE EXCEPTION 'provider request receipt is invalid'
        USING ERRCODE='22023';
    END IF;
    IF (
      (
        (request_receipt->>'estimated_input_tokens')::bigint
        * greatest(
          (request_receipt#>>'{pricing_rates,input_microusd_per_million_tokens}')::bigint,
          (request_receipt#>>'{pricing_rates,cached_input_microusd_per_million_tokens}')::bigint,
          (request_receipt#>>'{pricing_rates,cache_write_input_microusd_per_million_tokens}')::bigint
        ) + 999999
      ) / 1000000
      + (
        (request_receipt->>'max_output_tokens')::bigint
        * (request_receipt#>>'{pricing_rates,output_microusd_per_million_tokens}')::bigint
        + 999999
      ) / 1000000
    )<>(request_receipt->>'maximum_cost_microusd')::bigint THEN
      RAISE EXCEPTION 'provider request receipt cost binding is invalid'
        USING ERRCODE='23514';
    END IF;
    request_receipt_sha := encode(public.digest(
      convert_to(request_receipt::text,'UTF8'),'sha256'
    ),'hex');
  ELSIF request_receipt_text IS NOT NULL THEN
    RAISE EXCEPTION 'provider request receipt lacks reservation mode'
      USING ERRCODE='22023';
  END IF;

  SELECT event.* INTO replayed
  FROM memory.v5_extraction_call_event AS event
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.run_id<>p_run_id
       OR replayed.action NOT IN ('blocked','reserved')
       OR replayed.provider_id<>p_provider_id
       OR replayed.provider_version<>p_provider_version
       OR replayed.provider_model_sha256<>p_provider_model_sha256
       OR replayed.worker_id_sha256<>worker_sha
       OR replayed.rolling_window_seconds<>p_rolling_window_seconds
       OR replayed.max_reserved_calls<>p_max_reserved_calls
       OR replayed.failure_threshold<>p_failure_threshold
       OR (
         reservation_mode='memory_v1_openai_request_reservation_v1'
         AND NOT EXISTS (
           SELECT 1
           FROM memory.openai_provider_request_receipt_v1 AS receipt
           WHERE receipt.owner_user_id=actor
             AND receipt.receipt_id=replayed.event_id
             AND receipt.request_sha256=request_receipt->>'request_sha256'
             AND receipt.request_receipt_sha256=request_receipt_sha
             AND receipt.request_receipt=request_receipt
         )
       )
       OR (exact_target AND replayed.action='reserved' AND (
         replayed.job_id IS DISTINCT FROM exact_job
         OR NOT EXISTS (
           SELECT 1
           FROM memory.evidence_extraction_job AS replay_job
           WHERE replay_job.owner_user_id=actor
             AND replay_job.job_id=exact_job
             AND replay_job.evidence_content_sha256=exact_content_sha
         )
       )) THEN
      RAISE EXCEPTION 'bounded V5 extraction claim replay conflicts'
        USING ERRCODE='23514';
    END IF;
    IF replayed.action='blocked' THEN
      RETURN QUERY SELECT
        NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,p_route,NULL::integer,
        NULL::text,NULL::text,NULL::text,NULL::text,NULL::text,
        NULL::timestamptz,NULL::timestamptz,NULL::text,NULL::integer,
        NULL::text,'{}'::jsonb,NULL::uuid,replayed.outcome,
        replayed.reserved_calls_in_window,replayed.consecutive_rejections,
        'replayed'::text;
      RETURN;
    END IF;
    RETURN QUERY
    SELECT
      extraction_job.job_id,
      extraction_job.evidence_id,
      (claim_event.details->>'lease_token')::uuid,
      extraction_job.status::text,
      extraction_job.route,
      extraction_job.attempts,
      extraction_job.evidence_content_sha256,
      evidence.kind::text,
      evidence.source_system,
      evidence.external_id,
      evidence.content,
      evidence.observed_at,
      evidence.recorded_at,
      evidence.sensitivity::text,
      extraction_job.checkpoint_sequence,
      extraction_job.checkpoint_sha256,
      extraction_job.result,
      replayed.event_id,
      replayed.outcome,
      replayed.reserved_calls_in_window,
      replayed.consecutive_rejections,
      'replayed'::text
    FROM memory.evidence_extraction_job AS extraction_job
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=extraction_job.owner_user_id
     AND evidence.evidence_id=extraction_job.evidence_id
    JOIN memory.evidence_extraction_event AS claim_event
      ON claim_event.owner_user_id=extraction_job.owner_user_id
     AND claim_event.job_id=extraction_job.job_id
     AND claim_event.event_type='claimed'
     AND claim_event.operation_id=p_operation_id
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.job_id=replayed.job_id;
    RETURN;
  END IF;

  SELECT count(*)::integer INTO reserved_count
  FROM memory.v5_extraction_call_event AS event
  WHERE event.owner_user_id=actor
    AND event.action='reserved'
    AND event.created_at>=
      clock_timestamp()-make_interval(secs=>p_rolling_window_seconds);

  SELECT
    count(*)::integer,
    coalesce(bool_and(recent.outcome='rejected'),false)
  INTO latest_completion_count,latest_all_rejected
  FROM (
    SELECT event.outcome
    FROM memory.v5_extraction_call_event AS event
    WHERE event.owner_user_id=actor
      AND event.action='completed'
    ORDER BY event.created_at DESC,event.event_id DESC
    LIMIT p_failure_threshold
  ) AS recent;
  rejection_count := CASE
    WHEN latest_completion_count=p_failure_threshold
     AND latest_all_rejected THEN p_failure_threshold
    ELSE 0
  END;

  IF reserved_count>=p_max_reserved_calls THEN
    block_reason := 'quota_exhausted';
  ELSIF rejection_count>=p_failure_threshold THEN
    block_reason := 'circuit_open';
  END IF;
  IF block_reason IS NOT NULL THEN
    INSERT INTO memory.v5_extraction_call_event(
      event_id,owner_user_id,operation_id,run_id,action,outcome,
      provider_id,provider_version,provider_model_sha256,worker_id_sha256,
      external_model_calls,rejection_code,rolling_window_seconds,
      max_reserved_calls,failure_threshold,reserved_calls_in_window,
      consecutive_rejections
    ) VALUES (
      new_reservation_id,actor,p_operation_id,p_run_id,'blocked',block_reason,
      p_provider_id,p_provider_version,p_provider_model_sha256,worker_sha,
      0,block_reason,p_rolling_window_seconds,p_max_reserved_calls,
      p_failure_threshold,reserved_count,rejection_count
    );
    RETURN QUERY SELECT
      NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,p_route,NULL::integer,
      NULL::text,NULL::text,NULL::text,NULL::text,NULL::text,
      NULL::timestamptz,NULL::timestamptz,NULL::text,NULL::integer,
      NULL::text,'{}'::jsonb,NULL::uuid,block_reason,reserved_count,
      rejection_count,'applied'::text;
    RETURN;
  END IF;


  IF exact_target AND NOT EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_job AS exact_target_job
    JOIN memory.evidence AS target_evidence
      ON target_evidence.owner_user_id=exact_target_job.owner_user_id
     AND target_evidence.evidence_id=exact_target_job.evidence_id
    WHERE exact_target_job.owner_user_id=actor
      AND exact_target_job.job_id=exact_job
      AND exact_target_job.route=p_route
      AND exact_target_job.evidence_content_sha256=exact_content_sha
      AND target_evidence.content_sha256=exact_content_sha
      AND target_evidence.content IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'exact V5 extraction target is absent or mismatched'
      USING ERRCODE='23514';
  END IF;

  WITH exhausted AS (
    SELECT extraction_job.job_id,extraction_job.status AS prior_status
    FROM memory.evidence_extraction_job AS extraction_job
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.route=p_route
      AND (NOT exact_target OR (
        extraction_job.job_id=exact_job
        AND extraction_job.evidence_content_sha256=exact_content_sha
      ))
      AND extraction_job.attempts>=p_max_attempts
      AND (
        extraction_job.status IN ('pending','error')
        OR (extraction_job.status='processing'
          AND extraction_job.lease_expires_at<=clock_timestamp())
      )
    ORDER BY extraction_job.priority,extraction_job.available_at,
      extraction_job.created_at,extraction_job.job_id
    FOR UPDATE SKIP LOCKED
    LIMIT 100
  ), terminalized AS (
    UPDATE memory.evidence_extraction_job AS extraction_job
    SET status='skipped',lease_token=NULL,lease_expires_at=NULL,
      worker_id=p_worker_id,last_error='maximum extraction attempts exhausted'
    FROM exhausted
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.job_id=exhausted.job_id
    RETURNING extraction_job.job_id,exhausted.prior_status,
      extraction_job.attempts
  )
  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,from_status,to_status,
    actor_type,actor_ref,details
  )
  SELECT actor,terminalized.job_id,gen_random_uuid(),'skipped',
    terminalized.prior_status,'skipped','worker',p_worker_id,
    jsonb_build_object('reason','max_attempts_exhausted',
      'attempt',terminalized.attempts,'max_attempts',p_max_attempts)
  FROM terminalized;

  WITH selected AS (
    SELECT extraction_job.job_id,extraction_job.status AS prior_status
    FROM memory.evidence_extraction_job AS extraction_job
    WHERE extraction_job.owner_user_id=actor
      AND extraction_job.route=p_route
      AND (NOT exact_target OR (
        extraction_job.job_id=exact_job
        AND extraction_job.evidence_content_sha256=exact_content_sha
      ))
      AND extraction_job.available_at<=clock_timestamp()
      AND extraction_job.attempts<p_max_attempts
      AND (
        extraction_job.status IN ('pending','error')
        OR (extraction_job.status='processing'
          AND extraction_job.lease_expires_at<=clock_timestamp())
      )
    ORDER BY extraction_job.priority,extraction_job.available_at,
      extraction_job.created_at,extraction_job.job_id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
  )
  UPDATE memory.evidence_extraction_job AS extraction_job
  SET status='processing',attempts=extraction_job.attempts+1,
    lease_token=new_lease_token,
    lease_expires_at=clock_timestamp()+make_interval(secs=>p_lease_seconds),
    worker_id=p_worker_id,last_error=NULL
  FROM selected
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.job_id=selected.job_id
  RETURNING extraction_job.*,selected.prior_status INTO claimed;

  IF NOT FOUND THEN
    RETURN;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id=actor
      AND evidence.evidence_id=claimed.evidence_id
      AND evidence.content IS NOT NULL
      AND evidence.content_sha256=claimed.evidence_content_sha256
  ) THEN
    RAISE EXCEPTION 'claimed evidence content binding is invalid'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_extraction_call_event(
    event_id,owner_user_id,operation_id,run_id,job_id,action,outcome,
    provider_id,provider_version,provider_model_sha256,worker_id_sha256,
    external_model_calls,rolling_window_seconds,max_reserved_calls,
    failure_threshold,reserved_calls_in_window,consecutive_rejections
  ) VALUES (
    new_reservation_id,actor,p_operation_id,p_run_id,claimed.job_id,
    'reserved','reserved',p_provider_id,p_provider_version,
    p_provider_model_sha256,worker_sha,1,p_rolling_window_seconds,
    p_max_reserved_calls,p_failure_threshold,reserved_count+1,rejection_count
  );

  IF reservation_mode='memory_v1_openai_request_reservation_v1' THEN
    IF request_receipt->>'job_id'<>claimed.job_id::text
       OR request_receipt->>'evidence_content_sha256'
          <>claimed.evidence_content_sha256 THEN
      RAISE EXCEPTION 'provider request receipt changed after job claim'
        USING ERRCODE='23514';
    END IF;
    INSERT INTO memory.openai_provider_request_receipt_v1(
      receipt_id,owner_user_id,operation_id,run_id,job_id,evidence_id,
      request_sha256,request_receipt_sha256,request_receipt,
      provider_id,provider_version,provider_model_sha256,worker_id_sha256,
      maximum_cost_microusd
    ) VALUES (
      new_reservation_id,actor,p_operation_id,p_run_id,claimed.job_id,
      claimed.evidence_id,request_receipt->>'request_sha256',
      request_receipt_sha,memory_openai_extraction_authority_v2.request_receipt,
      p_provider_id,p_provider_version,
      p_provider_model_sha256,worker_sha,
      (request_receipt->>'maximum_cost_microusd')::bigint
    );
  END IF;

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,from_status,to_status,
    actor_type,actor_ref,details
  ) VALUES (
    actor,claimed.job_id,p_operation_id,'claimed',claimed.prior_status,
    'processing','worker',p_worker_id,
    jsonb_build_object('route',claimed.route,'lease_token',new_lease_token,
      'attempt',claimed.attempts,'lease_seconds',p_lease_seconds,
      'reclaimed',claimed.prior_status='processing',
      'call_reservation_event_id',new_reservation_id)
  );

  RETURN QUERY
  SELECT claimed.job_id,claimed.evidence_id,new_lease_token,
    claimed.status::text,claimed.route,claimed.attempts,
    claimed.evidence_content_sha256,evidence.kind::text,
    evidence.source_system,evidence.external_id,evidence.content,
    evidence.observed_at,evidence.recorded_at,evidence.sensitivity::text,
    claimed.checkpoint_sequence,claimed.checkpoint_sha256,claimed.result,
    new_reservation_id,'reserved'::text,reserved_count+1,rejection_count,
    'applied'::text
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=claimed.evidence_id;
END
$function$;
ALTER FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer) OWNER TO memory_v5_extraction_scheduler_maintainer;
REVOKE ALL ON FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer) FROM brains_app;
GRANT EXECUTE ON FUNCTION memory.claim_owner_v5_bounded_extraction_job_v1(uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer) TO brains_app;
