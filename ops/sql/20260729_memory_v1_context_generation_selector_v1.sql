BEGIN;

SELECT pg_advisory_xact_lock(
  hashtextextended('memory_v1_context_generation_selector_v1', 0)
);

DO $prerequisites$
BEGIN
  IF to_regclass('memory.evidence_contextual_span_v2') IS NULL
     OR to_regrole('memory_intake_maintainer') IS NULL
     OR to_regrole('memory_v5_local_reextract_maintainer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL THEN
    RAISE EXCEPTION
      'context generation selector prerequisites are absent';
  END IF;
  IF (SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
             OR rolinherit OR rolbypassrls
      FROM pg_roles
      WHERE rolname = 'memory_intake_maintainer') THEN
    RAISE EXCEPTION 'memory_intake_maintainer role is unsafe';
  END IF;
  IF (SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
             OR rolinherit OR rolbypassrls
      FROM pg_roles
      WHERE rolname = 'memory_v5_local_reextract_maintainer') THEN
    RAISE EXCEPTION
      'memory_v5_local_reextract_maintainer role is unsafe';
  END IF;
END
$prerequisites$;

CREATE OR REPLACE FUNCTION memory.select_owner_contextual_generation_v1(
  p_target_evidence_id uuid,
  p_max_spans integer
)
RETURNS TABLE(
  owner_user_id uuid,
  parent_evidence_id uuid,
  child_evidence_id uuid,
  source_id uuid,
  thread_id uuid,
  request_id uuid,
  splitter_version text,
  child_content_sha256 text,
  source_content_sha256 text,
  span_origin text,
  plan_sha256 text,
  ordinal smallint,
  source_ordinal integer,
  total_span_count integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, memory
SET row_security = on
AS $function$
DECLARE
  actor uuid := memory.current_actor_user_id();
  target_span memory.evidence_contextual_span_v2%ROWTYPE;
BEGIN
  IF actor IS NULL
     OR p_target_evidence_id IS NULL
     OR p_max_spans IS NULL
     OR p_max_spans < 1
     OR p_max_spans > 32 THEN
    RAISE EXCEPTION 'context generation selector inputs are invalid'
      USING ERRCODE = '23514';
  END IF;

  SELECT span.*
  INTO target_span
  FROM memory.evidence_contextual_span_v2 AS span
  WHERE span.owner_user_id = actor
    AND span.child_evidence_id = p_target_evidence_id;

  IF NOT FOUND THEN
    RETURN;
  END IF;

  RETURN QUERY
  WITH ranked AS (
    SELECT
      span.*,
      row_number() OVER (
        ORDER BY span.ordinal, span.child_evidence_id
      )::integer AS ranked_ordinal,
      count(*) OVER ()::integer AS ranked_total
    FROM memory.evidence_contextual_span_v2 AS span
    WHERE span.owner_user_id = actor
      AND span.parent_evidence_id = target_span.parent_evidence_id
      AND span.source_id = target_span.source_id
      AND span.source_content_sha256 =
            target_span.source_content_sha256
      AND span.splitter_version = target_span.splitter_version
      AND span.plan_sha256 = target_span.plan_sha256
      AND span.span_origin = target_span.span_origin
  ),
  target_position AS (
    SELECT ranked.ranked_ordinal, ranked.ranked_total
    FROM ranked
    WHERE ranked.child_evidence_id = p_target_evidence_id
  ),
  bounds AS (
    SELECT greatest(
             1,
             least(
               target_position.ranked_ordinal -
                 ((p_max_spans - 1) * 2 / 3),
               greatest(
                 target_position.ranked_total - p_max_spans + 1,
                 1
               )
             )
           ) AS window_start
    FROM target_position
  )
  SELECT
    ranked.owner_user_id,
    ranked.parent_evidence_id,
    ranked.child_evidence_id,
    ranked.source_id,
    ranked.thread_id,
    ranked.request_id,
    ranked.splitter_version,
    ranked.child_content_sha256,
    ranked.source_content_sha256,
    ranked.span_origin,
    ranked.plan_sha256,
    ranked.ordinal,
    ranked.ranked_ordinal,
    ranked.ranked_total
  FROM ranked
  CROSS JOIN bounds
  WHERE ranked.ranked_ordinal BETWEEN
        bounds.window_start AND bounds.window_start + p_max_spans - 1
  ORDER BY ranked.ranked_ordinal;
END
$function$;

ALTER FUNCTION memory.select_owner_contextual_generation_v1(uuid,integer)
  OWNER TO memory_intake_maintainer;

REVOKE ALL ON FUNCTION
  memory.select_owner_contextual_generation_v1(uuid,integer)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION
  memory.select_owner_contextual_generation_v1(uuid,integer)
  FROM brains_app,memory_v5_local_reextract_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.select_owner_contextual_generation_v1(uuid,integer)
  TO brains_app,memory_v5_local_reextract_maintainer;

CREATE OR REPLACE FUNCTION
memory.plan_owner_context_generation_retry_v1(
  p_source_job_id uuid DEFAULT NULL
)
RETURNS TABLE(
  source_job_id uuid,
  evidence_id uuid,
  evidence_content_sha256 text,
  source_attempts integer,
  source_selector_version text,
  source_result_sha256 text,
  parent_evidence_id uuid,
  splitter_version text,
  generation_plan_sha256 text,
  reason_code text,
  next_selector_version text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, memory
SET row_security = on
AS $function$
DECLARE
  actor uuid := memory.current_actor_user_id();
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION
      'context generation retry plan requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;

  RETURN QUERY
  SELECT
    job.job_id,
    job.evidence_id,
    job.evidence_content_sha256,
    job.attempts,
    job.selector_version,
    encode(
      public.digest(convert_to(job.result::text, 'UTF8'), 'sha256'),
      'hex'
    ),
    generation.parent_evidence_id,
    generation.splitter_version,
    generation.plan_sha256,
    'context_generation_overlap_corrected'::text,
    '20260729_v5_context_generation_retry_v1'::text
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = job.owner_user_id
   AND evidence.evidence_id = job.evidence_id
  JOIN LATERAL memory.select_owner_contextual_generation_v1(
    job.evidence_id,
    32
  ) AS generation
    ON generation.child_evidence_id = job.evidence_id
  WHERE job.owner_user_id = actor
    AND (p_source_job_id IS NULL OR job.job_id = p_source_job_id)
    AND job.selector_version = '20260729_v4_contextual_resplit'
    AND job.status = 'skipped'
    AND job.attempts = 1
    AND job.route = 'relational_extraction'
    AND job.lease_token IS NULL
    AND job.lease_expires_at IS NULL
    AND job.last_error IS NULL
    AND job.result #>> '{final,status}' = 'skipped'
    AND job.result #>> '{final,payload,outcome_class}' =
          'record_terminal'
    AND job.result #>> '{final,payload,disposition}' = 'deferred'
    AND job.result #>> '{final,payload,reason_code}' =
          'context_missing'
    AND evidence.status = 'active'
    AND evidence.source_system = 'public.chat_log'
    AND evidence.content_sha256 = job.evidence_content_sha256
    AND generation.splitter_version =
          'memory_v1_contextual_span_splitter_20260728_v3'
    AND generation.span_origin = 'contextual_split_v3'
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_packet_v5_local AS packet
      WHERE packet.owner_user_id = actor
        AND packet.job_id = job.job_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_job AS retry
      WHERE retry.owner_user_id = actor
        AND retry.evidence_id = job.evidence_id
        AND retry.selector_version =
              '20260729_v5_context_generation_retry_v1'
    )
  ORDER BY job.evidence_id, job.job_id;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.enqueue_owner_context_generation_retry_v1(
  p_operation_id uuid,
  p_source_job_id uuid,
  p_new_job_id uuid,
  p_new_terminal_id uuid,
  p_expected_content_sha256 text,
  p_selector_version text
)
RETURNS TABLE(
  job_id uuid,
  intake_terminal_id uuid,
  status text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, memory
SET row_security = on
AS $function$
DECLARE
  actor uuid := memory.current_actor_user_id();
  planned record;
  replay_event memory.evidence_extraction_event%ROWTYPE;
  replay_job memory.evidence_extraction_job%ROWTYPE;
  replay_terminal memory.evidence_intake_terminal%ROWTYPE;
  fingerprint text;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION
      'context generation retry apply requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_source_job_id IS NULL
     OR p_new_job_id IS NULL
     OR p_new_terminal_id IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_selector_version <>
          '20260729_v5_context_generation_retry_v1' THEN
    RAISE EXCEPTION 'context generation retry inputs are invalid'
      USING ERRCODE = '22023';
  END IF;

  PERFORM pg_advisory_xact_lock(
    hashtextextended(
      concat_ws(
        '|',
        'memory_v1_context_generation_retry_v1',
        actor::text,
        p_source_job_id::text,
        p_selector_version
      ),
      0
    )
  );

  SELECT event.*
  INTO replay_event
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id = actor
    AND event.operation_id = p_operation_id;

  IF FOUND THEN
    SELECT value.*
    INTO replay_job
    FROM memory.evidence_extraction_job AS value
    WHERE value.owner_user_id = actor
      AND value.job_id = p_new_job_id;
    SELECT value.*
    INTO replay_terminal
    FROM memory.evidence_intake_terminal AS value
    WHERE value.owner_user_id = actor
      AND value.terminal_id = p_new_terminal_id;

    IF replay_job.job_id IS NULL
       OR replay_terminal.terminal_id IS NULL
       OR replay_event.job_id <> p_new_job_id
       OR replay_event.event_type <> 'queued'
       OR replay_event.from_status IS NOT NULL
       OR replay_event.to_status <> 'pending'
       OR replay_event.actor_type <> 'admin'
       OR replay_event.actor_ref IS DISTINCT FROM
            'context_generation_retry_v1'
       OR replay_event.details->>'source_job_id'
            IS DISTINCT FROM p_source_job_id::text
       OR replay_event.details->>'expected_content_sha256'
            IS DISTINCT FROM p_expected_content_sha256
       OR replay_job.intake_terminal_id <> p_new_terminal_id
       OR replay_job.evidence_id <> replay_terminal.evidence_id
       OR replay_job.selector_version <> p_selector_version
       OR replay_job.evidence_content_sha256 <>
            p_expected_content_sha256
       OR replay_job.route <> 'relational_extraction'
       OR replay_terminal.selector_version <> p_selector_version
       OR replay_terminal.evidence_content_sha256 <>
            p_expected_content_sha256
       OR replay_terminal.outcome <> 'dispatched'
       OR replay_terminal.reason_code <> 'eligible_dispatched' THEN
      RAISE EXCEPTION 'context generation retry replay conflicts'
        USING ERRCODE = '23514';
    END IF;

    RETURN QUERY
    SELECT
      replay_job.job_id,
      replay_terminal.terminal_id,
      replay_job.status::text,
      'replayed'::text;
    RETURN;
  END IF;

  SELECT *
  INTO planned
  FROM memory.plan_owner_context_generation_retry_v1(p_source_job_id);

  IF NOT FOUND
     OR planned.evidence_content_sha256 <>
          p_expected_content_sha256
     OR planned.reason_code <>
          'context_generation_overlap_corrected'
     OR planned.next_selector_version <> p_selector_version THEN
    RAISE EXCEPTION 'context generation retry plan changed'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_job AS value
    WHERE value.owner_user_id = actor
      AND (
        value.job_id = p_new_job_id
        OR (
          value.evidence_id = planned.evidence_id
          AND value.selector_version = p_selector_version
        )
      )
  )
  OR EXISTS (
    SELECT 1
    FROM memory.evidence_intake_terminal AS value
    WHERE value.owner_user_id = actor
      AND (
        value.terminal_id = p_new_terminal_id
        OR (
          value.evidence_id = planned.evidence_id
          AND value.selector_version = p_selector_version
        )
      )
  ) THEN
    RAISE EXCEPTION 'context generation retry identifiers conflict'
      USING ERRCODE = '23514';
  END IF;

  fingerprint := encode(
    public.digest(
      convert_to(
        jsonb_build_object(
          'owner_user_id', actor,
          'source_job_id', planned.source_job_id,
          'evidence_id', planned.evidence_id,
          'selector_version', p_selector_version,
          'evidence_content_sha256', p_expected_content_sha256,
          'source_result_sha256', planned.source_result_sha256,
          'generation_plan_sha256', planned.generation_plan_sha256,
          'reason_code', planned.reason_code
        )::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );

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
  )
  VALUES (
    p_new_terminal_id,
    actor,
    planned.evidence_id,
    p_selector_version,
    'dispatched',
    'eligible_dispatched',
    p_expected_content_sha256,
    fingerprint,
    actor,
    session_user,
    jsonb_build_object(
      'route', 'relational_extraction',
      'extraction_job_id', p_new_job_id,
      'reextract_reason', planned.reason_code,
      'source_job_id', planned.source_job_id,
      'source_selector_version', planned.source_selector_version,
      'source_attempts', planned.source_attempts,
      'source_result_sha256', planned.source_result_sha256,
      'splitter_version', planned.splitter_version,
      'generation_plan_sha256', planned.generation_plan_sha256,
      'source_prose_copied', false
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
  )
  VALUES (
    p_new_job_id,
    actor,
    planned.evidence_id,
    p_new_terminal_id,
    p_selector_version,
    p_expected_content_sha256,
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
  )
  VALUES (
    actor,
    p_new_job_id,
    p_operation_id,
    'queued',
    NULL,
    'pending',
    'admin',
    'context_generation_retry_v1',
    jsonb_build_object(
      'source_job_id', planned.source_job_id,
      'expected_content_sha256', p_expected_content_sha256,
      'source_result_sha256', planned.source_result_sha256,
      'splitter_version', planned.splitter_version,
      'generation_plan_sha256', planned.generation_plan_sha256,
      'reason_code', planned.reason_code,
      'source_prose_copied', false
    )
  );

  RETURN QUERY
  SELECT
    p_new_job_id,
    p_new_terminal_id,
    'pending'::text,
    'applied'::text;
END
$function$;

GRANT USAGE ON SCHEMA memory
  TO memory_v5_local_reextract_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_local_reextract_maintainer;
GRANT EXECUTE ON FUNCTION public.digest(bytea,text)
  TO memory_v5_local_reextract_maintainer;

ALTER FUNCTION
  memory.plan_owner_context_generation_retry_v1(uuid)
  OWNER TO memory_v5_local_reextract_maintainer;
ALTER FUNCTION
  memory.enqueue_owner_context_generation_retry_v1(
    uuid,uuid,uuid,uuid,text,text
  )
  OWNER TO memory_v5_local_reextract_maintainer;

REVOKE ALL ON FUNCTION
  memory.plan_owner_context_generation_retry_v1(uuid)
  FROM PUBLIC,brains_app,memory_v5_local_reextract_maintainer;
REVOKE ALL ON FUNCTION
  memory.enqueue_owner_context_generation_retry_v1(
    uuid,uuid,uuid,uuid,text,text
  )
  FROM PUBLIC,brains_app,memory_v5_local_reextract_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_context_generation_retry_v1(uuid)
  TO brains_app,memory_v5_local_reextract_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.enqueue_owner_context_generation_retry_v1(
    uuid,uuid,uuid,uuid,text,text
  )
  TO brains_app;

COMMIT;
