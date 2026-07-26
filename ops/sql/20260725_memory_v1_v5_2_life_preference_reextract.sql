BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $preflight$
BEGIN
  IF current_user <> 'sage'
     OR to_regrole('memory_v5_local_reextract_maintainer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('public.digest(bytea,text)') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass(
       'memory.evidence_extraction_packet_v5_local'
     ) IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_intake_terminal') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL THEN
    RAISE EXCEPTION
      'V5.2 life-preference re-extraction prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION
memory.enqueue_owner_v5_2_life_preference_reextract_v1(
  p_operation_id uuid,
  p_job_id uuid,
  p_terminal_id uuid,
  p_evidence_id uuid,
  p_expected_content_sha256 text,
  p_prior_packet_id uuid,
  p_expected_prior_packet_storage_sha256 text,
  p_manifest_sha256 text,
  p_expected_provider_source_sha256 text
)
RETURNS TABLE(
  job_id uuid,
  terminal_id uuid,
  status text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  selector constant text :=
    '20260725_v5_2_life_preference_reextract_v1';
  contract constant text :=
    'memory_v1_v5_2_life_preference_reextract_v1';
  content_sha constant text :=
    '524bda462b5ece2b507249a11ea68c02ae0ecd4d7148d21ccec4df7bbf675ec7';
  prior_packet_id constant uuid :=
    '83598662-cb38-5e30-abe6-1ed8e57b0d87'::uuid;
  prior_packet_storage_sha constant text :=
    'ea1ece347f77e1eb013c6d98078cf2c61893e54027d9094997ceeca53c5bc6a1';
  provider_source_sha constant text :=
    '12531e9617f6d77017d6500ace01b4821599ebdd833e6a4d44a9eb8ceb5e3e73';
  compiler_sha constant text :=
    'f82e6f4339dfe4aada7e5c3edb71fde8125a819f33677b3f47b98f3726b60419';
  target_evidence_id constant uuid :=
    '51b03105-4acc-5ded-b703-e139a892db9d'::uuid;
  evidence_record memory.evidence%ROWTYPE;
  prior_packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  prior_job memory.evidence_extraction_job%ROWTYPE;
  existing_job memory.evidence_extraction_job%ROWTYPE;
  existing_terminal memory.evidence_intake_terminal%ROWTYPE;
  decision_fingerprint text;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION
      'V5.2 life-preference re-extraction requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_job_id IS NULL
     OR p_terminal_id IS NULL
     OR p_evidence_id IS DISTINCT FROM target_evidence_id
     OR p_expected_content_sha256 IS DISTINCT FROM content_sha
     OR p_prior_packet_id IS DISTINCT FROM prior_packet_id
     OR p_expected_prior_packet_storage_sha256
        IS DISTINCT FROM prior_packet_storage_sha
     OR p_manifest_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_provider_source_sha256
        IS DISTINCT FROM provider_source_sha THEN
    RAISE EXCEPTION
      'V5.2 life-preference re-extraction inputs are invalid'
      USING ERRCODE = '22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|', actor::text, p_evidence_id::text, selector),
    0
  ));

  SELECT evidence.* INTO evidence_record
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id = actor
    AND evidence.evidence_id = target_evidence_id;
  IF NOT FOUND
     OR evidence_record.status <> 'active'
     OR evidence_record.source_system <> 'public.chat_log'
     OR evidence_record.content_sha256 IS DISTINCT FROM content_sha THEN
    RAISE EXCEPTION
      'V5.2 life-preference evidence changed or is not owner-scoped'
      USING ERRCODE = '23514';
  END IF;

  SELECT packet.* INTO prior_packet
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id = actor
    AND packet.packet_id = prior_packet_id
    AND packet.evidence_id = target_evidence_id;
  IF NOT FOUND
     OR prior_packet.evidence_content_sha256 IS DISTINCT FROM content_sha
     OR prior_packet.packet_storage_sha256
        IS DISTINCT FROM prior_packet_storage_sha
     OR prior_packet.policy_compiler_sha256
        IS DISTINCT FROM compiler_sha
     OR prior_packet.provider_id <> 'local_llama_cpp'
     OR prior_packet.provider_version <> 'v1'
     OR prior_packet.local_model_calls <> 1
     OR prior_packet.external_model_calls <> 0
     OR prior_packet.normalized_packet->>'contract_version'
        <> 'memory_v1_relational_extraction_v5_2'
     OR prior_packet.normalized_packet->>'predicate_registry_version'
        <> 'memory_predicate_registry_v5_2'
     OR jsonb_array_length(
       prior_packet.normalized_packet->'entity_mentions'
     ) <> 0
     OR jsonb_array_length(
       prior_packet.normalized_packet->'observations'
     ) <> 0
     OR jsonb_array_length(
       prior_packet.normalized_packet->'comparison_hints'
     ) <> 0
     OR jsonb_array_length(
       prior_packet.normalized_packet->'deferrals'
     ) <> 1
     OR prior_packet.normalized_packet#>>'{deferrals,0,reason_code}'
        <> 'insufficient_evidence' THEN
    RAISE EXCEPTION
      'V5.2 life-preference prior packet changed'
      USING ERRCODE = '23514';
  END IF;

  SELECT job.* INTO prior_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id = actor
    AND job.job_id = prior_packet.job_id
    AND job.evidence_id = target_evidence_id;
  IF NOT FOUND
     OR prior_job.status <> 'review_required'
     OR prior_job.selector_version <> '20260717_v2'
     OR prior_job.attempts <> 1 THEN
    RAISE EXCEPTION
      'V5.2 life-preference prior review state changed'
      USING ERRCODE = '23514';
  END IF;

  SELECT job.* INTO existing_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id = actor
    AND job.evidence_id = target_evidence_id
    AND job.selector_version = selector;

  SELECT terminal.* INTO existing_terminal
  FROM memory.evidence_intake_terminal AS terminal
  WHERE terminal.owner_user_id = actor
    AND terminal.evidence_id = target_evidence_id
    AND terminal.selector_version = selector;

  IF existing_job.job_id IS NOT NULL
     OR existing_terminal.terminal_id IS NOT NULL THEN
    IF existing_job.job_id IS DISTINCT FROM p_job_id
       OR existing_terminal.terminal_id IS DISTINCT FROM p_terminal_id
       OR existing_job.intake_terminal_id
          IS DISTINCT FROM existing_terminal.terminal_id
       OR existing_job.evidence_content_sha256
          IS DISTINCT FROM content_sha
       OR existing_terminal.evidence_content_sha256
          IS DISTINCT FROM content_sha
       OR existing_job.route <> 'relational_extraction'
       OR existing_job.intake_reason_code <> 'eligible_unprocessed'
       OR existing_terminal.outcome <> 'dispatched'
       OR existing_terminal.reason_code <> 'eligible_dispatched'
       OR existing_terminal.details->>'reextract_contract'
          IS DISTINCT FROM contract
       OR existing_terminal.details->>'operation_id'
          IS DISTINCT FROM p_operation_id::text
       OR existing_terminal.details->>'manifest_sha256'
          IS DISTINCT FROM p_manifest_sha256
       OR existing_terminal.details->>'provider_source_sha256'
          IS DISTINCT FROM provider_source_sha
       OR existing_terminal.details->>'prior_packet_id'
          IS DISTINCT FROM prior_packet_id::text
       OR existing_terminal.details->>'prior_packet_storage_sha256'
          IS DISTINCT FROM prior_packet_storage_sha THEN
      RAISE EXCEPTION
        'V5.2 life-preference re-extraction replay conflicts'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY
    SELECT
      existing_job.job_id,
      existing_terminal.terminal_id,
      existing_job.status::text,
      'replayed'::text;
    RETURN;
  END IF;

  IF EXISTS (
       SELECT 1
       FROM memory.evidence_extraction_job
       WHERE evidence_extraction_job.job_id = p_job_id
     )
     OR EXISTS (
       SELECT 1
       FROM memory.evidence_intake_terminal
       WHERE evidence_intake_terminal.terminal_id = p_terminal_id
     )
     OR EXISTS (
       SELECT 1
       FROM memory.evidence_extraction_event
       WHERE evidence_extraction_event.operation_id = p_operation_id
     ) THEN
    RAISE EXCEPTION
      'V5.2 life-preference re-extraction ID collision'
      USING ERRCODE = '23514';
  END IF;

  decision_fingerprint := encode(public.digest(convert_to(
    jsonb_build_object(
      'contract_version', contract,
      'owner_user_id', actor,
      'operation_id', p_operation_id,
      'job_id', p_job_id,
      'terminal_id', p_terminal_id,
      'evidence_id', target_evidence_id,
      'evidence_content_sha256', content_sha,
      'selector_version', selector,
      'manifest_sha256', p_manifest_sha256,
      'provider_source_sha256', provider_source_sha,
      'prior_packet_id', prior_packet_id,
      'prior_packet_storage_sha256', prior_packet_storage_sha
    )::text,
    'UTF8'
  ), 'sha256'), 'hex');

  INSERT INTO memory.evidence_intake_terminal(
    terminal_id,
    owner_user_id,
    evidence_id,
    selector_version,
    outcome,
    reason_code,
    evidence_content_sha256,
    decision_fingerprint,
    actor_user_id,
    invoked_by_role,
    details
  ) VALUES (
    p_terminal_id,
    actor,
    target_evidence_id,
    selector,
    'dispatched',
    'eligible_dispatched',
    content_sha,
    decision_fingerprint,
    actor,
    session_user,
    jsonb_build_object(
      'route', 'relational_extraction',
      'extraction_job_id', p_job_id,
      'plan_reason_code', 'eligible_unprocessed',
      'reextract_contract', contract,
      'operation_id', p_operation_id,
      'manifest_sha256', p_manifest_sha256,
      'provider_source_sha256', provider_source_sha,
      'prior_packet_id', prior_packet_id,
      'prior_packet_storage_sha256', prior_packet_storage_sha
    )
  );

  INSERT INTO memory.evidence_extraction_job(
    job_id,
    owner_user_id,
    evidence_id,
    intake_terminal_id,
    selector_version,
    evidence_content_sha256,
    route,
    intake_reason_code,
    status
  ) VALUES (
    p_job_id,
    actor,
    target_evidence_id,
    p_terminal_id,
    selector,
    content_sha,
    'relational_extraction',
    'eligible_unprocessed',
    'pending'
  );

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,
    job_id,
    operation_id,
    event_type,
    from_status,
    to_status,
    actor_type,
    actor_ref,
    details
  ) VALUES (
    actor,
    p_job_id,
    p_operation_id,
    'queued',
    NULL,
    'pending',
    'system',
    contract,
    jsonb_build_object(
      'intake_terminal_id', p_terminal_id,
      'selector_version', selector,
      'route', 'relational_extraction',
      'manifest_sha256', p_manifest_sha256,
      'provider_source_sha256', provider_source_sha,
      'prior_packet_id', prior_packet_id,
      'prior_packet_storage_sha256', prior_packet_storage_sha
    )
  );

  RETURN QUERY
  SELECT
    p_job_id,
    p_terminal_id,
    'pending'::text,
    'applied'::text;
END
$function$;

ALTER FUNCTION
memory.enqueue_owner_v5_2_life_preference_reextract_v1(
  uuid,
  uuid,
  uuid,
  uuid,
  text,
  uuid,
  text,
  text,
  text
) OWNER TO memory_v5_local_reextract_maintainer;

REVOKE ALL ON FUNCTION
memory.enqueue_owner_v5_2_life_preference_reextract_v1(
  uuid,
  uuid,
  uuid,
  uuid,
  text,
  uuid,
  text,
  text,
  text
) FROM PUBLIC, brains_app, memory_v5_local_reextract_maintainer;

GRANT EXECUTE ON FUNCTION
memory.enqueue_owner_v5_2_life_preference_reextract_v1(
  uuid,
  uuid,
  uuid,
  uuid,
  text,
  uuid,
  text,
  text,
  text
) TO brains_app;

COMMIT;
