BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '180s';

DO $preflight$
BEGIN
  IF current_user <> 'sage'
     OR to_regrole('brains_app') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure(
       'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)'
     ) IS NULL
     OR to_regprocedure(
       'memory.enqueue_owner_evidence_extraction_v1(uuid,text,text,text,text)'
     ) IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass(
       'memory.evidence_extraction_packet_v5_local'
     ) IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regclass('public.chat_log') IS NULL THEN
    RAISE EXCEPTION 'legacy context rebind prerequisites are absent';
  END IF;
  IF to_regrole('memory_context_rebind_maintainer') IS NULL THEN
    CREATE ROLE memory_context_rebind_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOBYPASSRLS;
  END IF;
END
$preflight$;

CREATE TABLE memory.evidence_context_rebind_v1 (
  rebind_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  source_job_id uuid NOT NULL,
  source_evidence_id uuid NOT NULL,
  rebound_job_id uuid NOT NULL,
  rebound_evidence_id uuid NOT NULL,
  raw_source_id uuid NOT NULL,
  source_content_sha256 text NOT NULL,
  selector_version text NOT NULL,
  record_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, rebind_id),
  UNIQUE (owner_user_id, source_job_id),
  UNIQUE (owner_user_id, rebound_job_id),
  UNIQUE (owner_user_id, rebound_evidence_id),
  FOREIGN KEY (owner_user_id, source_job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id, job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, source_evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, rebound_job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id, job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, rebound_evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  CHECK (
    source_evidence_id <> rebound_evidence_id
    AND source_job_id <> rebound_job_id
  ),
  CHECK (
    selector_version = '20260728_v5_2_legacy_context_rebind_v1'
  ),
  CHECK (source_content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (record_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX evidence_context_rebind_v1_owner_time_idx
  ON memory.evidence_context_rebind_v1(
    owner_user_id, created_at, rebind_id
  );

CREATE OR REPLACE FUNCTION
memory.guard_evidence_context_rebind_append_only_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'evidence context rebind history is append-only'
    USING ERRCODE = '42501';
END
$function$;

CREATE TRIGGER evidence_context_rebind_append_only_v1
BEFORE UPDATE OR DELETE ON memory.evidence_context_rebind_v1
FOR EACH ROW
EXECUTE FUNCTION memory.guard_evidence_context_rebind_append_only_v1();

ALTER TABLE memory.evidence_context_rebind_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.evidence_context_rebind_v1 FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_isolation
ON memory.evidence_context_rebind_v1
USING (
  owner_user_id = (SELECT memory.current_actor_user_id())
)
WITH CHECK (
  owner_user_id = (SELECT memory.current_actor_user_id())
);

GRANT USAGE ON SCHEMA memory TO memory_context_rebind_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_context_rebind_maintainer;
GRANT SELECT ON
  memory.evidence,
  memory.evidence_extraction_job,
  memory.evidence_extraction_packet_v5_local,
  memory.evidence_context_rebind_v1,
  memory.relational_stage_batch,
  public.chat_log
TO memory_context_rebind_maintainer;
GRANT UPDATE ON memory.evidence_extraction_job
  TO memory_context_rebind_maintainer;
GRANT INSERT ON
  memory.evidence_extraction_event,
  memory.evidence_context_rebind_v1
TO memory_context_rebind_maintainer;

CREATE OR REPLACE FUNCTION
memory.finalize_owner_legacy_context_rebind_v1(
  p_operation_id uuid,
  p_source_job_id uuid,
  p_rebound_evidence_id uuid,
  p_rebound_job_id uuid,
  p_raw_source_id uuid,
  p_expected_content_sha256 text,
  p_selector_version text
)
RETURNS TABLE(
  rebind_id uuid,
  source_job_id uuid,
  rebound_job_id uuid,
  source_status text,
  record_sha256 text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
  prior memory.evidence_context_rebind_v1%ROWTYPE;
  source_job memory.evidence_extraction_job%ROWTYPE;
  rebound_job memory.evidence_extraction_job%ROWTYPE;
  source_evidence memory.evidence%ROWTYPE;
  rebound_evidence memory.evidence%ROWTYPE;
  raw_source public.chat_log%ROWTYPE;
  record_hash text;
  final_result jsonb;
BEGIN
  IF session_user <> 'brains_app'
     OR current_user <> 'memory_context_rebind_maintainer' THEN
    RAISE EXCEPTION 'legacy context rebind requires brains_app'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_source_job_id IS NULL
     OR p_rebound_evidence_id IS NULL
     OR p_rebound_job_id IS NULL
     OR p_raw_source_id IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_selector_version <>
       '20260728_v5_2_legacy_context_rebind_v1' THEN
    RAISE EXCEPTION 'legacy context rebind inputs are invalid'
      USING ERRCODE = '22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws(
      '|',
      'memory_v1_legacy_context_rebind_v1',
      actor::text,
      p_source_job_id::text
    ),
    0
  ));

  SELECT value.* INTO prior
  FROM memory.evidence_context_rebind_v1 AS value
  WHERE value.owner_user_id = actor
    AND (
      value.rebind_id = p_operation_id
      OR value.source_job_id = p_source_job_id
    );
  IF FOUND THEN
    IF prior.rebind_id <> p_operation_id
       OR prior.source_job_id <> p_source_job_id
       OR prior.rebound_evidence_id <> p_rebound_evidence_id
       OR prior.rebound_job_id <> p_rebound_job_id
       OR prior.raw_source_id <> p_raw_source_id
       OR prior.source_content_sha256 <>
          p_expected_content_sha256
       OR prior.selector_version <> p_selector_version THEN
      RAISE EXCEPTION 'legacy context rebind replay conflicts'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT
      prior.rebind_id,
      prior.source_job_id,
      prior.rebound_job_id,
      'skipped'::text,
      prior.record_sha256,
      'replayed'::text;
    RETURN;
  END IF;

  SELECT job.* INTO source_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id = actor
    AND job.job_id = p_source_job_id
  FOR UPDATE;
  IF NOT FOUND
     OR source_job.selector_version <> '20260717_v2'
     OR source_job.route <> 'relational_extraction'
     OR source_job.status NOT IN ('pending', 'error')
     OR source_job.lease_token IS NOT NULL
     OR source_job.lease_expires_at IS NOT NULL
     OR source_job.evidence_content_sha256 <>
        p_expected_content_sha256 THEN
    RAISE EXCEPTION 'legacy source job changed'
      USING ERRCODE = '23514';
  END IF;

  SELECT evidence.* INTO source_evidence
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id = actor
    AND evidence.evidence_id = source_job.evidence_id;
  IF NOT FOUND
     OR source_evidence.status <> 'active'
     OR source_evidence.source_system <> 'public.chat_log'
     OR source_evidence.content IS NULL
     OR source_evidence.content = ''
     OR source_evidence.content_sha256 <>
        p_expected_content_sha256
     OR (
       source_evidence.metadata ? 'source_id'
       AND source_evidence.metadata ? 'source_content_sha256'
       AND source_evidence.metadata ? 'source_char_start'
       AND source_evidence.metadata ? 'source_char_end'
     ) THEN
    RAISE EXCEPTION 'legacy source evidence changed'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
       SELECT 1
       FROM memory.evidence_extraction_packet_v5_local AS packet
       WHERE packet.owner_user_id = actor
         AND packet.job_id = source_job.job_id
     )
     OR EXISTS (
       SELECT 1
       FROM memory.relational_stage_batch AS stage
       WHERE stage.owner_user_id = actor
         AND stage.evidence_id = source_evidence.evidence_id
     ) THEN
    RAISE EXCEPTION
      'legacy source already has downstream processing'
      USING ERRCODE = '23514';
  END IF;

  SELECT log.* INTO raw_source
  FROM public.chat_log AS log
  WHERE log.owner_user_id = actor
    AND log.id = p_raw_source_id;
  IF NOT FOUND
     OR raw_source.thread_id IS NULL
     OR raw_source.request_id IS NULL
     OR source_evidence.external_id NOT IN (
       raw_source.id::text,
       'chat_log:' || raw_source.id::text
     )
     OR raw_source.text IS DISTINCT FROM source_evidence.content
     OR encode(
       public.digest(convert_to(raw_source.text, 'UTF8'), 'sha256'),
       'hex'
     ) <> p_expected_content_sha256 THEN
    RAISE EXCEPTION 'legacy raw source binding changed'
      USING ERRCODE = '23514';
  END IF;

  SELECT evidence.* INTO rebound_evidence
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id = actor
    AND evidence.evidence_id = p_rebound_evidence_id;
  IF NOT FOUND
     OR rebound_evidence.status <> 'active'
     OR rebound_evidence.source_system <> 'public.chat_log'
     OR rebound_evidence.external_id <>
       'context_rebind_v1:' || source_evidence.evidence_id::text
     OR rebound_evidence.content IS DISTINCT FROM raw_source.text
     OR rebound_evidence.content_sha256 <>
        p_expected_content_sha256
     OR rebound_evidence.metadata->>'source_id' <>
        raw_source.id::text
     OR rebound_evidence.metadata->>'source_content_sha256' <>
        p_expected_content_sha256
     OR rebound_evidence.metadata->>'thread_id' <>
        raw_source.thread_id::text
     OR rebound_evidence.metadata->>'request_id' <>
        raw_source.request_id::text
     OR rebound_evidence.metadata->>'source_char_start' <> '0'
     OR rebound_evidence.metadata->>'source_char_end' <>
        char_length(raw_source.text)::text
     OR rebound_evidence.metadata->>'primary_lane' <>
        'unclassified_user_statement'
     OR rebound_evidence.metadata->>'epistemic_role' <>
        'user_report_unclassified'
     OR rebound_evidence.metadata->>'span_origin' <>
        'legacy_full_turn_rebind_v1'
     OR rebound_evidence.metadata->>'rebind_from_evidence_id' <>
        source_evidence.evidence_id::text
     OR rebound_evidence.metadata->>'rebind_from_job_id' <>
        source_job.job_id::text THEN
    RAISE EXCEPTION 'rebound evidence binding changed'
      USING ERRCODE = '23514';
  END IF;

  SELECT job.* INTO rebound_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id = actor
    AND job.job_id = p_rebound_job_id;
  IF NOT FOUND
     OR rebound_job.evidence_id <> rebound_evidence.evidence_id
     OR rebound_job.selector_version <> p_selector_version
     OR rebound_job.evidence_content_sha256 <>
        p_expected_content_sha256
     OR rebound_job.route <> 'relational_extraction'
     OR rebound_job.intake_reason_code <> 'eligible_unprocessed'
     OR rebound_job.status <> 'pending'
     OR rebound_job.attempts <> 0
     OR rebound_job.lease_token IS NOT NULL
     OR rebound_job.lease_expires_at IS NOT NULL THEN
    RAISE EXCEPTION 'rebound extraction job changed'
      USING ERRCODE = '23514';
  END IF;

  record_hash := encode(public.digest(convert_to(
    jsonb_build_object(
      'contract_version', 'memory_v1_legacy_context_rebind_v1',
      'owner_user_id', actor,
      'rebind_id', p_operation_id,
      'source_job_id', source_job.job_id,
      'source_evidence_id', source_evidence.evidence_id,
      'rebound_job_id', rebound_job.job_id,
      'rebound_evidence_id', rebound_evidence.evidence_id,
      'raw_source_id', raw_source.id,
      'source_content_sha256', p_expected_content_sha256,
      'selector_version', p_selector_version
    )::text,
    'UTF8'
  ), 'sha256'), 'hex');

  final_result := jsonb_set(
    COALESCE(source_job.result, '{}'::jsonb),
    '{final}',
    jsonb_build_object(
      'status', 'skipped',
      'sha256', record_hash,
      'payload', jsonb_build_object(
        'reason_code', 'context_rebound',
        'rebind_id', p_operation_id,
        'rebound_evidence_id', rebound_evidence.evidence_id,
        'rebound_job_id', rebound_job.job_id
      )
    ),
    true
  );
  IF pg_column_size(final_result) > 32768 THEN
    RAISE EXCEPTION 'legacy rebind job result exceeds budget'
      USING ERRCODE = '22023';
  END IF;

  UPDATE memory.evidence_extraction_job AS job
  SET status = 'skipped',
      lease_token = NULL,
      lease_expires_at = NULL,
      worker_id = 'memory_v1_legacy_context_rebind_v1',
      last_error = NULL,
      result = final_result
  WHERE job.owner_user_id = actor
    AND job.job_id = source_job.job_id;

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
    source_job.job_id,
    p_operation_id,
    'skipped',
    source_job.status,
    'skipped',
    'system',
    'memory_v1_legacy_context_rebind_v1',
    jsonb_build_object(
      'result_sha256', record_hash,
      'reason_code', 'context_rebound',
      'rebound_evidence_id', rebound_evidence.evidence_id,
      'rebound_job_id', rebound_job.job_id
    )
  );

  INSERT INTO memory.evidence_context_rebind_v1(
    rebind_id,
    owner_user_id,
    source_job_id,
    source_evidence_id,
    rebound_job_id,
    rebound_evidence_id,
    raw_source_id,
    source_content_sha256,
    selector_version,
    record_sha256
  ) VALUES (
    p_operation_id,
    actor,
    source_job.job_id,
    source_evidence.evidence_id,
    rebound_job.job_id,
    rebound_evidence.evidence_id,
    raw_source.id,
    p_expected_content_sha256,
    p_selector_version,
    record_hash
  );

  RETURN QUERY SELECT
    p_operation_id,
    source_job.job_id,
    rebound_job.job_id,
    'skipped'::text,
    record_hash,
    'applied'::text;
END
$function$;

ALTER FUNCTION memory.guard_evidence_context_rebind_append_only_v1()
  OWNER TO memory_context_rebind_maintainer;
ALTER TABLE memory.evidence_context_rebind_v1
  OWNER TO memory_context_rebind_maintainer;
ALTER FUNCTION memory.finalize_owner_legacy_context_rebind_v1(
  uuid,uuid,uuid,uuid,uuid,text,text
) OWNER TO memory_context_rebind_maintainer;

REVOKE ALL ON TABLE memory.evidence_context_rebind_v1
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION
  memory.guard_evidence_context_rebind_append_only_v1()
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION
  memory.finalize_owner_legacy_context_rebind_v1(
    uuid,uuid,uuid,uuid,uuid,text,text
  )
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.finalize_owner_legacy_context_rebind_v1(
    uuid,uuid,uuid,uuid,uuid,text,text
  )
  TO brains_app;

COMMENT ON TABLE memory.evidence_context_rebind_v1 IS
  'Append-only provenance linking legacy context-free evidence/jobs to source-bound replacements.';
COMMENT ON FUNCTION
memory.finalize_owner_legacy_context_rebind_v1(
  uuid,uuid,uuid,uuid,uuid,text,text
) IS
  'Finalizes one exact same-owner full-turn context rebind and supersedes only the pending legacy job.';

COMMIT;
