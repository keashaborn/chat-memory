BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'owner memory workbench migration requires sage';
  END IF;
  IF to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.v5_local_packet_supersession') IS NULL
     OR to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regclass('memory.v5_local_terminal_reconciliation_v1') IS NULL
     OR to_regclass('memory.v5_2_atom_admission_proposal') IS NULL
     OR to_regclass('memory.v5_local_packet_stage_admission') IS NULL
     OR to_regrole('brains_app') IS NULL THEN
    RAISE EXCEPTION 'owner memory workbench prerequisites are absent';
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.owner_packet_feedback_v1 (
  feedback_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  packet_storage_sha256 text NOT NULL
    CHECK (packet_storage_sha256 ~ '^[0-9a-f]{64}$'),
  decision text NOT NULL CHECK (decision IN ('correct','not_correct')),
  diagnostic_note text,
  diagnostic_note_sha256 text,
  supersedes_feedback_id uuid,
  policy_version text NOT NULL
    CHECK (policy_version='memory_owner_packet_feedback_v1'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,feedback_id),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,supersedes_feedback_id),
  FOREIGN KEY(owner_user_id,packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(
      owner_user_id,packet_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,supersedes_feedback_id)
    REFERENCES memory.owner_packet_feedback_v1(
      owner_user_id,feedback_id
    ) ON DELETE RESTRICT,
  CHECK (
    (diagnostic_note IS NULL AND diagnostic_note_sha256 IS NULL)
    OR (
      diagnostic_note IS NOT NULL
      AND length(diagnostic_note) BETWEEN 1 AND 2000
      AND diagnostic_note_sha256 ~ '^[0-9a-f]{64}$'
    )
  )
);

CREATE INDEX IF NOT EXISTS owner_packet_feedback_v1_owner_time_idx
  ON memory.owner_packet_feedback_v1(
    owner_user_id,created_at DESC,feedback_id DESC
  );
CREATE INDEX IF NOT EXISTS owner_packet_feedback_v1_owner_packet_idx
  ON memory.owner_packet_feedback_v1(
    owner_user_id,packet_id,created_at DESC,feedback_id DESC
  );

ALTER TABLE memory.owner_packet_feedback_v1 OWNER TO sage;
ALTER TABLE memory.owner_packet_feedback_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.owner_packet_feedback_v1 FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.owner_packet_feedback_v1;
CREATE POLICY owner_isolation ON memory.owner_packet_feedback_v1
  USING (owner_user_id=(SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP TRIGGER IF EXISTS owner_packet_feedback_v1_append_only_guard
  ON memory.owner_packet_feedback_v1;
CREATE TRIGGER owner_packet_feedback_v1_append_only_guard
BEFORE UPDATE OR DELETE ON memory.owner_packet_feedback_v1
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

CREATE OR REPLACE FUNCTION memory.owner_memory_workbench_summary_v1()
RETURNS TABLE(
  total_count bigint,
  pending_count bigint,
  correct_count bigint,
  not_correct_count bigint
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
    RAISE EXCEPTION 'owner memory workbench summary requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;

  RETURN QUERY
  WITH eligible AS (
    SELECT packet.packet_id
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
  ),
  current_feedback AS (
    SELECT feedback.packet_id,feedback.decision
    FROM memory.owner_packet_feedback_v1 AS feedback
    JOIN eligible ON eligible.packet_id=feedback.packet_id
    WHERE feedback.owner_user_id=actor
      AND NOT EXISTS (
        SELECT 1
        FROM memory.owner_packet_feedback_v1 AS successor
        WHERE successor.owner_user_id=actor
          AND successor.supersedes_feedback_id=feedback.feedback_id
      )
  )
  SELECT
    count(*)::bigint,
    count(*) FILTER (WHERE current_feedback.packet_id IS NULL)::bigint,
    count(*) FILTER (WHERE current_feedback.decision='correct')::bigint,
    count(*) FILTER (WHERE current_feedback.decision='not_correct')::bigint
  FROM eligible
  LEFT JOIN current_feedback USING(packet_id);
END
$function$;

CREATE OR REPLACE FUNCTION memory.list_owner_memory_workbench_v1(
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
  packet_created_at timestamptz,
  manual_review_required boolean,
  normalized_packet jsonb,
  route text,
  route_reason_code text,
  feedback_id uuid,
  feedback_decision text,
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
    packet.created_at,
    packet.manual_review_required,
    packet.normalized_packet,
    route.route,
    route.reason_code,
    feedback.feedback_id,
    feedback.decision,
    feedback.diagnostic_note,
    feedback.created_at
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

CREATE OR REPLACE FUNCTION memory.record_owner_memory_workbench_feedback_v1(
  p_operation_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
  p_decision text,
  p_diagnostic_note text DEFAULT NULL
)
RETURNS TABLE(
  feedback_id uuid,
  decision text,
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

  normalized_note := NULLIF(btrim(p_diagnostic_note),'');
  IF p_operation_id IS NULL
     OR p_packet_id IS NULL
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_decision NOT IN ('correct','not_correct')
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
    concat_ws('|','owner_memory_workbench_v1',actor::text,p_packet_id::text),0
  ));

  SELECT candidate.* INTO replay
  FROM memory.owner_packet_feedback_v1 AS candidate
  WHERE candidate.owner_user_id=actor
    AND candidate.operation_id=p_operation_id;
  IF FOUND THEN
    IF replay.packet_id<>p_packet_id
       OR replay.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR replay.decision<>p_decision
       OR replay.diagnostic_note_sha256 IS DISTINCT FROM normalized_note_sha256
       OR replay.diagnostic_note IS DISTINCT FROM normalized_note THEN
      RAISE EXCEPTION 'owner memory workbench feedback replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      replay.feedback_id,replay.decision,replay.diagnostic_note,
      replay.created_at,'replayed'::text;
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
     AND prior.diagnostic_note_sha256 IS NOT DISTINCT FROM normalized_note_sha256
     AND prior.diagnostic_note IS NOT DISTINCT FROM normalized_note THEN
    RETURN QUERY SELECT
      prior.feedback_id,prior.decision,prior.diagnostic_note,
      prior.created_at,'unchanged'::text;
    RETURN;
  END IF;

  INSERT INTO memory.owner_packet_feedback_v1(
    feedback_id,owner_user_id,operation_id,packet_id,
    packet_storage_sha256,decision,diagnostic_note,
    diagnostic_note_sha256,supersedes_feedback_id,policy_version
  ) VALUES (
    gen_random_uuid(),actor,p_operation_id,p_packet_id,
    p_expected_packet_storage_sha256,p_decision,normalized_note,
    normalized_note_sha256,prior.feedback_id,'memory_owner_packet_feedback_v1'
  )
  RETURNING * INTO inserted;

  RETURN QUERY SELECT
    inserted.feedback_id,inserted.decision,inserted.diagnostic_note,
    inserted.created_at,'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_owner_packet_feedback_promotion_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.owner_packet_feedback_v1 AS feedback
    WHERE feedback.owner_user_id=NEW.owner_user_id
      AND feedback.packet_id=NEW.packet_id
      AND feedback.decision='not_correct'
      AND NOT EXISTS (
        SELECT 1
        FROM memory.owner_packet_feedback_v1 AS successor
        WHERE successor.owner_user_id=feedback.owner_user_id
          AND successor.supersedes_feedback_id=feedback.feedback_id
      )
  ) THEN
    RAISE EXCEPTION 'owner-rejected packet cannot enter staging'
      USING ERRCODE='42501';
  END IF;
  RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS owner_packet_feedback_stage_guard
  ON memory.v5_local_packet_stage_admission;
CREATE TRIGGER owner_packet_feedback_stage_guard
BEFORE INSERT ON memory.v5_local_packet_stage_admission
FOR EACH ROW EXECUTE FUNCTION memory.guard_owner_packet_feedback_promotion_v1();

DROP TRIGGER IF EXISTS owner_packet_feedback_atom_guard
  ON memory.v5_2_atom_admission_proposal;
CREATE TRIGGER owner_packet_feedback_atom_guard
BEFORE INSERT ON memory.v5_2_atom_admission_proposal
FOR EACH ROW EXECUTE FUNCTION memory.guard_owner_packet_feedback_promotion_v1();

ALTER FUNCTION memory.owner_memory_workbench_summary_v1() OWNER TO sage;
ALTER FUNCTION memory.list_owner_memory_workbench_v1(
  text,integer,timestamptz,uuid
) OWNER TO sage;
ALTER FUNCTION memory.record_owner_memory_workbench_feedback_v1(
  uuid,uuid,text,text,text
) OWNER TO sage;
ALTER FUNCTION memory.guard_owner_packet_feedback_promotion_v1() OWNER TO sage;

REVOKE ALL ON memory.owner_packet_feedback_v1 FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.owner_memory_workbench_summary_v1()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.list_owner_memory_workbench_v1(
  text,integer,timestamptz,uuid
) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.record_owner_memory_workbench_feedback_v1(
  uuid,uuid,text,text,text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.guard_owner_packet_feedback_promotion_v1()
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.owner_memory_workbench_summary_v1()
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.list_owner_memory_workbench_v1(
  text,integer,timestamptz,uuid
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.record_owner_memory_workbench_feedback_v1(
  uuid,uuid,text,text,text
) TO brains_app;

COMMIT;
