BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'owner memory workbench diagnostics migration requires sage';
  END IF;
  IF to_regclass('memory.owner_packet_feedback_v1') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('public.chat_log') IS NULL
     OR to_regrole('brains_app') IS NULL THEN
    RAISE EXCEPTION 'owner memory workbench diagnostics prerequisites are absent';
  END IF;
END
$preflight$;

ALTER TABLE memory.owner_packet_feedback_v1
  ADD COLUMN IF NOT EXISTS diagnostic_category text;

DO $constraints$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid='memory.owner_packet_feedback_v1'::regclass
      AND conname='owner_packet_feedback_v1_diagnostic_category_check'
  ) THEN
    ALTER TABLE memory.owner_packet_feedback_v1
      ADD CONSTRAINT owner_packet_feedback_v1_diagnostic_category_check
      CHECK (
        diagnostic_category IS NULL
        OR diagnostic_category IN (
          'context_missing',
          'duplicate_or_repeat',
          'missed_durable_information',
          'incomplete_compound_extraction',
          'incorrect_entity_or_relationship',
          'incorrect_time_or_status',
          'uncertainty_or_attribution_error',
          'wrong_memory_lane',
          'should_not_be_memory',
          'transcription_ambiguity',
          'other'
        )
      );
  END IF;
END
$constraints$;

ALTER TABLE memory.owner_packet_feedback_v1
  DROP CONSTRAINT IF EXISTS owner_packet_feedback_v1_policy_version_check;
ALTER TABLE memory.owner_packet_feedback_v1
  ADD CONSTRAINT owner_packet_feedback_v1_policy_version_check
  CHECK (
    policy_version IN (
      'memory_owner_packet_feedback_v1',
      'memory_owner_packet_feedback_v2'
    )
  );

CREATE OR REPLACE FUNCTION memory.list_owner_memory_workbench_v2(
  p_state text DEFAULT 'pending',
  p_limit integer DEFAULT 12,
  p_before_created_at timestamptz DEFAULT NULL,
  p_before_packet_id uuid DEFAULT NULL
)
RETURNS TABLE(
  packet_id uuid,
  packet_storage_sha256 text,
  job_id uuid,
  evidence_id uuid,
  source_content text,
  source_recorded_at timestamptz,
  source_context_content text,
  source_char_start integer,
  source_char_end integer,
  packet_created_at timestamptz,
  manual_review_required boolean,
  normalized_packet jsonb,
  route text,
  route_reason_code text,
  feedback_id uuid,
  feedback_decision text,
  feedback_category text,
  feedback_note text,
  feedback_created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'owner memory workbench list requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_state NOT IN ('pending','reviewed','all')
     OR p_limit NOT BETWEEN 1 AND 25
     OR ((p_before_created_at IS NULL)<>(p_before_packet_id IS NULL)) THEN
    RAISE EXCEPTION 'owner memory workbench list inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    packet.packet_id,
    packet.packet_storage_sha256,
    packet.job_id,
    packet.evidence_id,
    evidence.content,
    evidence.recorded_at,
    source_context.text,
    CASE
      WHEN evidence.metadata->>'source_char_start' ~ '^[0-9]+$'
      THEN (evidence.metadata->>'source_char_start')::integer
      ELSE NULL
    END,
    CASE
      WHEN evidence.metadata->>'source_char_end' ~ '^[0-9]+$'
      THEN (evidence.metadata->>'source_char_end')::integer
      ELSE NULL
    END,
    packet.created_at,
    packet.manual_review_required,
    packet.normalized_packet,
    route.route,
    route.reason_code,
    feedback.feedback_id,
    feedback.decision,
    feedback.diagnostic_category,
    feedback.diagnostic_note,
    feedback.created_at
  FROM memory.evidence_extraction_packet_v5_local AS packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=packet.owner_user_id
   AND job.job_id=packet.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=packet.owner_user_id
   AND evidence.evidence_id=packet.evidence_id
  LEFT JOIN public.chat_log AS source_context
    ON source_context.owner_user_id=actor
   AND source_context.id=CASE
     WHEN evidence.metadata->>'source_id'
       ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     THEN (evidence.metadata->>'source_id')::uuid
     ELSE NULL
   END
  LEFT JOIN memory.v5_2_local_packet_route_event AS route
    ON route.owner_user_id=packet.owner_user_id
   AND route.packet_id=packet.packet_id
  LEFT JOIN LATERAL (
    SELECT candidate.*
    FROM memory.owner_packet_feedback_v1 AS candidate
    WHERE candidate.owner_user_id=actor
      AND candidate.packet_id=packet.packet_id
      AND NOT EXISTS (
        SELECT 1
        FROM memory.owner_packet_feedback_v1 AS successor
        WHERE successor.owner_user_id=actor
          AND successor.supersedes_feedback_id=candidate.feedback_id
      )
    ORDER BY candidate.created_at DESC,candidate.feedback_id DESC
    LIMIT 1
  ) AS feedback ON true
  WHERE packet.owner_user_id=actor
    AND job.status='review_required'
    AND evidence.status='active'
    AND (
      route.route='manual_review_artifact_ready'
      OR packet.manual_review_required
      OR packet.entity_mention_count>0
      OR packet.observation_count>0
      OR packet.comparison_hint_count>0
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_packet_supersession AS supersession
      WHERE supersession.owner_user_id=actor
        AND supersession.prior_packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_packet_disposition AS disposition
      WHERE disposition.owner_user_id=actor
        AND disposition.packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_terminal_reconciliation_v1 AS reconciliation
      WHERE reconciliation.owner_user_id=actor
        AND reconciliation.packet_id=packet.packet_id
    )
    AND (
      p_state='all'
      OR (p_state='pending' AND feedback.feedback_id IS NULL)
      OR (p_state='reviewed' AND feedback.feedback_id IS NOT NULL)
    )
    AND (
      p_before_created_at IS NULL
      OR (packet.created_at,packet.packet_id)
         < (p_before_created_at,p_before_packet_id)
    )
  ORDER BY packet.created_at DESC,packet.packet_id DESC
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.record_owner_memory_workbench_feedback_v2(
  p_operation_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
  p_decision text,
  p_diagnostic_category text DEFAULT NULL,
  p_diagnostic_note text DEFAULT NULL
)
RETURNS TABLE(
  feedback_id uuid,
  decision text,
  diagnostic_category text,
  diagnostic_note text,
  created_at timestamptz,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  normalized_category text;
  normalized_note text;
  normalized_note_sha256 text;
  packet_row record;
  prior memory.owner_packet_feedback_v1%ROWTYPE;
  replay memory.owner_packet_feedback_v1%ROWTYPE;
  inserted memory.owner_packet_feedback_v1%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'owner memory workbench feedback requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;

  normalized_category := NULLIF(btrim(p_diagnostic_category),'');
  normalized_note := NULLIF(btrim(p_diagnostic_note),'');
  IF p_operation_id IS NULL
     OR p_packet_id IS NULL
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_decision NOT IN ('correct','not_correct')
     OR (
       p_decision='correct'
       AND normalized_category IS NOT NULL
     )
     OR (
       p_decision='not_correct'
       AND (
         normalized_category IS NULL
         OR normalized_category NOT IN (
           'context_missing',
           'duplicate_or_repeat',
           'missed_durable_information',
           'incomplete_compound_extraction',
           'incorrect_entity_or_relationship',
           'incorrect_time_or_status',
           'uncertainty_or_attribution_error',
           'wrong_memory_lane',
           'should_not_be_memory',
           'transcription_ambiguity',
           'other'
         )
       )
     )
     OR length(coalesce(normalized_note,''))>2000 THEN
    RAISE EXCEPTION 'owner memory workbench feedback inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  normalized_note_sha256 := CASE
    WHEN normalized_note IS NULL THEN NULL
    ELSE encode(
      public.digest(convert_to(normalized_note,'UTF8'),'sha256'),'hex'
    )
  END;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','owner_memory_workbench_v2',actor::text,p_packet_id::text),0
  ));

  SELECT candidate.* INTO replay
  FROM memory.owner_packet_feedback_v1 AS candidate
  WHERE candidate.owner_user_id=actor
    AND candidate.operation_id=p_operation_id;
  IF FOUND THEN
    IF replay.packet_id<>p_packet_id
       OR replay.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR replay.decision<>p_decision
       OR replay.diagnostic_category IS DISTINCT FROM normalized_category
       OR replay.diagnostic_note_sha256 IS DISTINCT FROM normalized_note_sha256
       OR replay.diagnostic_note IS DISTINCT FROM normalized_note THEN
      RAISE EXCEPTION 'owner memory workbench feedback replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      replay.feedback_id,replay.decision,replay.diagnostic_category,
      replay.diagnostic_note,replay.created_at,'replayed'::text;
    RETURN;
  END IF;

  SELECT
    packet.packet_id,
    packet.packet_storage_sha256
  INTO packet_row
  FROM memory.evidence_extraction_packet_v5_local AS packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=packet.owner_user_id
   AND job.job_id=packet.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=packet.owner_user_id
   AND evidence.evidence_id=packet.evidence_id
  LEFT JOIN memory.v5_2_local_packet_route_event AS route
    ON route.owner_user_id=packet.owner_user_id
   AND route.packet_id=packet.packet_id
  WHERE packet.owner_user_id=actor
    AND packet.packet_id=p_packet_id
    AND packet.packet_storage_sha256=p_expected_packet_storage_sha256
    AND job.status='review_required'
    AND evidence.status='active'
    AND (
      route.route='manual_review_artifact_ready'
      OR packet.manual_review_required
      OR packet.entity_mention_count>0
      OR packet.observation_count>0
      OR packet.comparison_hint_count>0
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_packet_supersession AS supersession
      WHERE supersession.owner_user_id=actor
        AND supersession.prior_packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_packet_disposition AS disposition
      WHERE disposition.owner_user_id=actor
        AND disposition.packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_terminal_reconciliation_v1 AS reconciliation
      WHERE reconciliation.owner_user_id=actor
        AND reconciliation.packet_id=packet.packet_id
    );
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner memory workbench packet is unavailable'
      USING ERRCODE='22023';
  END IF;

  SELECT candidate.* INTO prior
  FROM memory.owner_packet_feedback_v1 AS candidate
  WHERE candidate.owner_user_id=actor
    AND candidate.packet_id=p_packet_id
    AND NOT EXISTS (
      SELECT 1
      FROM memory.owner_packet_feedback_v1 AS successor
      WHERE successor.owner_user_id=actor
        AND successor.supersedes_feedback_id=candidate.feedback_id
    )
  ORDER BY candidate.created_at DESC,candidate.feedback_id DESC
  LIMIT 1;

  IF FOUND
     AND prior.decision=p_decision
     AND prior.diagnostic_category IS NOT DISTINCT FROM normalized_category
     AND prior.diagnostic_note_sha256 IS NOT DISTINCT FROM normalized_note_sha256
     AND prior.diagnostic_note IS NOT DISTINCT FROM normalized_note THEN
    RETURN QUERY SELECT
      prior.feedback_id,prior.decision,prior.diagnostic_category,
      prior.diagnostic_note,prior.created_at,'unchanged'::text;
    RETURN;
  END IF;

  INSERT INTO memory.owner_packet_feedback_v1(
    feedback_id,owner_user_id,operation_id,packet_id,
    packet_storage_sha256,decision,diagnostic_category,diagnostic_note,
    diagnostic_note_sha256,supersedes_feedback_id,policy_version
  ) VALUES (
    gen_random_uuid(),actor,p_operation_id,p_packet_id,
    p_expected_packet_storage_sha256,p_decision,normalized_category,
    normalized_note,normalized_note_sha256,prior.feedback_id,
    'memory_owner_packet_feedback_v2'
  )
  RETURNING * INTO inserted;

  RETURN QUERY SELECT
    inserted.feedback_id,inserted.decision,inserted.diagnostic_category,
    inserted.diagnostic_note,inserted.created_at,'applied'::text;
END
$function$;

ALTER FUNCTION memory.list_owner_memory_workbench_v2(
  text,integer,timestamptz,uuid
) OWNER TO sage;
ALTER FUNCTION memory.record_owner_memory_workbench_feedback_v2(
  uuid,uuid,text,text,text,text
) OWNER TO sage;

REVOKE ALL ON FUNCTION memory.list_owner_memory_workbench_v2(
  text,integer,timestamptz,uuid
) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.record_owner_memory_workbench_feedback_v2(
  uuid,uuid,text,text,text,text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.list_owner_memory_workbench_v2(
  text,integer,timestamptz,uuid
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.record_owner_memory_workbench_feedback_v2(
  uuid,uuid,text,text,text,text
) TO brains_app;

COMMIT;
