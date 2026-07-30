BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'queue reconciliation migration requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regrole('memory_v5_local_inference_maintainer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure(
       'memory.guard_v5_local_inference_append_only()'
     ) IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.v5_local_inference_event') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5') IS NULL THEN
    RAISE EXCEPTION 'queue reconciliation prerequisites are absent';
  END IF;
  IF to_regrole('memory_context_queue_reconciliation_maintainer') IS NULL THEN
    CREATE ROLE memory_context_queue_reconciliation_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOBYPASSRLS;
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.evidence_context_queue_reconciliation_v1 (
  reconciliation_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  source_job_id uuid NOT NULL,
  source_evidence_id uuid NOT NULL,
  terminal_job_id uuid NOT NULL,
  terminal_evidence_id uuid NOT NULL,
  source_envelope_sha256 text NOT NULL
    CHECK (source_envelope_sha256 ~ '^[0-9a-f]{64}$'),
  reason_code text NOT NULL
    CHECK (reason_code='superseded_by_terminal_source_envelope'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (source_job_id<>terminal_job_id),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,source_job_id),
  FOREIGN KEY(owner_user_id,source_job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id,job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,terminal_job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id,job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,source_evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,terminal_evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT
);
ALTER TABLE memory.evidence_context_queue_reconciliation_v1 OWNER TO sage;
ALTER TABLE memory.evidence_context_queue_reconciliation_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.evidence_context_queue_reconciliation_v1
  FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS context_queue_reconciliation_read
  ON memory.evidence;
CREATE POLICY context_queue_reconciliation_read ON memory.evidence
  FOR SELECT TO memory_context_queue_reconciliation_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS context_queue_reconciliation_job
  ON memory.evidence_extraction_job;
CREATE POLICY context_queue_reconciliation_job
  ON memory.evidence_extraction_job
  TO memory_context_queue_reconciliation_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS context_queue_reconciliation_event
  ON memory.evidence_extraction_event;
CREATE POLICY context_queue_reconciliation_event
  ON memory.evidence_extraction_event
  FOR INSERT TO memory_context_queue_reconciliation_maintainer
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS owner_isolation
  ON memory.evidence_context_queue_reconciliation_v1;
CREATE POLICY owner_isolation
  ON memory.evidence_context_queue_reconciliation_v1
  TO memory_context_queue_reconciliation_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());

DROP TRIGGER IF EXISTS evidence_context_queue_reconciliation_append_only
  ON memory.evidence_context_queue_reconciliation_v1;
CREATE TRIGGER evidence_context_queue_reconciliation_append_only
BEFORE UPDATE OR DELETE
  ON memory.evidence_context_queue_reconciliation_v1
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

GRANT USAGE ON SCHEMA memory
  TO memory_context_queue_reconciliation_maintainer;
GRANT SELECT ON memory.evidence
  TO memory_context_queue_reconciliation_maintainer;
GRANT SELECT,UPDATE ON memory.evidence_extraction_job
  TO memory_context_queue_reconciliation_maintainer;
GRANT INSERT ON memory.evidence_extraction_event
  TO memory_context_queue_reconciliation_maintainer;
GRANT SELECT,INSERT ON memory.evidence_context_queue_reconciliation_v1
  TO memory_context_queue_reconciliation_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_context_queue_reconciliation_maintainer;

CREATE OR REPLACE FUNCTION memory.plan_owner_context_superseded_v1(
  p_limit integer DEFAULT 100
)
RETURNS TABLE(
  source_job_id uuid,
  source_evidence_id uuid,
  terminal_job_id uuid,
  terminal_evidence_id uuid,
  source_envelope_sha256 text,
  source_created_at timestamptz
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
    RAISE EXCEPTION 'context supersession plan requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 100 THEN
    RAISE EXCEPTION 'context supersession limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT job.job_id,evidence.evidence_id,
    terminal.job_id,terminal.evidence_id,
    encode(public.digest(convert_to(jsonb_build_object(
      'owner_user_id',actor,
      'source_job_id',job.job_id,
      'source_evidence_id',evidence.evidence_id,
      'terminal_job_id',terminal.job_id,
      'terminal_evidence_id',terminal.evidence_id,
      'source_id',evidence.metadata->>'source_id',
      'source_content_sha256',
        evidence.metadata->>'source_content_sha256',
      'source_char_start',evidence.metadata->>'source_char_start',
      'source_char_end',evidence.metadata->>'source_char_end'
    )::text,'UTF8'),'sha256'),'hex'),
    job.created_at
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=job.owner_user_id
   AND evidence.evidence_id=job.evidence_id
  JOIN LATERAL (
    SELECT sibling.job_id,sibling.evidence_id
    FROM memory.evidence_extraction_job AS sibling
    JOIN memory.evidence AS sibling_evidence
      ON sibling_evidence.owner_user_id=sibling.owner_user_id
     AND sibling_evidence.evidence_id=sibling.evidence_id
    WHERE sibling.owner_user_id=job.owner_user_id
      AND sibling.route=job.route
      AND sibling.job_id<>job.job_id
      AND sibling.status IN ('review_required','completed','skipped')
      AND sibling.created_at>=job.created_at
      AND sibling_evidence.metadata->>'source_id'
          =evidence.metadata->>'source_id'
      AND sibling_evidence.metadata->>'source_content_sha256'
          =evidence.metadata->>'source_content_sha256'
      AND sibling_evidence.metadata->>'source_char_start'
          =evidence.metadata->>'source_char_start'
      AND sibling_evidence.metadata->>'source_char_end'
          =evidence.metadata->>'source_char_end'
    ORDER BY sibling.created_at DESC,sibling.job_id DESC
    LIMIT 1
  ) AS terminal ON true
  WHERE job.owner_user_id=actor
    AND job.route='relational_extraction'
    AND job.status IN ('pending','error')
    AND job.attempts<2
    AND job.available_at<=clock_timestamp()
    AND job.lease_token IS NULL
    AND job.lease_expires_at IS NULL
    AND evidence.status='active'
    AND evidence.metadata->>'source_id'
      ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    AND evidence.metadata->>'source_content_sha256' ~ '^[0-9a-f]{64}$'
    AND evidence.metadata->>'source_char_start' ~ '^[0-9]+$'
    AND evidence.metadata->>'source_char_end' ~ '^[0-9]+$'
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_context_queue_reconciliation_v1 AS prior
      WHERE prior.owner_user_id=actor
        AND prior.source_job_id=job.job_id
    )
  ORDER BY job.created_at,job.job_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_local_orphan_v1(
  p_limit integer DEFAULT 100
)
RETURNS TABLE(
  job_id uuid,
  evidence_content_sha256 text,
  job_created_at timestamptz,
  reservation_event_id uuid
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
    RAISE EXCEPTION 'local orphan plan requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 100 THEN
    RAISE EXCEPTION 'local orphan plan limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT job.job_id,job.evidence_content_sha256,job.created_at,
    reservation.event_id
  FROM memory.evidence_extraction_job AS job
  JOIN LATERAL (
    SELECT event.event_id,event.created_at
    FROM memory.v5_local_inference_event AS event
    WHERE event.owner_user_id=job.owner_user_id
      AND event.job_id=job.job_id
      AND event.action='reserved'
      AND event.outcome='reserved'
    ORDER BY event.created_at DESC,event.event_id DESC
    LIMIT 1
  ) AS reservation ON true
  WHERE job.owner_user_id=actor
    AND job.route='relational_extraction'
    AND job.status='processing'
    AND job.attempts>=1
    AND job.lease_token IS NOT NULL
    AND job.lease_expires_at<=clock_timestamp()-interval '60 seconds'
    AND job.worker_id LIKE
          'memory_v1_v5_local_inference_scheduler_v2:%'
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_inference_event AS completion
      WHERE completion.owner_user_id=job.owner_user_id
        AND completion.reservation_event_id=reservation.event_id
        AND completion.action='completed'
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_inference_event AS later
      WHERE later.owner_user_id=job.owner_user_id
        AND later.job_id=job.job_id
        AND later.action='reserved'
        AND (later.created_at,later.event_id)>
            (reservation.created_at,reservation.event_id)
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_packet_v5_local AS packet
      WHERE packet.owner_user_id=job.owner_user_id
        AND packet.job_id=job.job_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_packet_v5 AS packet
      WHERE packet.owner_user_id=job.owner_user_id
        AND packet.job_id=job.job_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_event AS recovery
      WHERE recovery.owner_user_id=job.owner_user_id
        AND recovery.job_id=job.job_id
        AND recovery.actor_ref='memory_v1_v5_local_orphan_recovery_v1'
    )
  ORDER BY job.created_at,job.job_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.finalize_owner_context_superseded_v1(
  p_reconciliation_id uuid,
  p_operation_id uuid,
  p_source_job_id uuid,
  p_source_evidence_id uuid,
  p_terminal_job_id uuid,
  p_terminal_evidence_id uuid,
  p_expected_source_envelope_sha256 text
)
RETURNS TABLE(
  source_job_id uuid,
  terminal_job_id uuid,
  status text,
  apply_outcome text,
  rows_written integer
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  planned record;
  replayed memory.evidence_context_queue_reconciliation_v1%ROWTYPE;
  prior_status text;
  new_event_id uuid:=gen_random_uuid();
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'context supersession finalizer requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_reconciliation_id IS NULL OR p_operation_id IS NULL
     OR p_source_job_id IS NULL OR p_source_evidence_id IS NULL
     OR p_terminal_job_id IS NULL OR p_terminal_evidence_id IS NULL
     OR p_source_job_id=p_terminal_job_id
     OR p_expected_source_envelope_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'context supersession inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT value.* INTO replayed
  FROM memory.evidence_context_queue_reconciliation_v1 AS value
  WHERE value.owner_user_id=actor
    AND (value.reconciliation_id=p_reconciliation_id
      OR value.operation_id=p_operation_id
      OR value.source_job_id=p_source_job_id);
  IF FOUND THEN
    IF replayed.reconciliation_id<>p_reconciliation_id
       OR replayed.operation_id<>p_operation_id
       OR replayed.source_job_id<>p_source_job_id
       OR replayed.source_evidence_id<>p_source_evidence_id
       OR replayed.terminal_job_id<>p_terminal_job_id
       OR replayed.terminal_evidence_id<>p_terminal_evidence_id
       OR replayed.source_envelope_sha256
            <>p_expected_source_envelope_sha256 THEN
      RAISE EXCEPTION 'context supersession replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT p_source_job_id,p_terminal_job_id,
      'skipped'::text,'replayed'::text,0;
    RETURN;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_context_superseded',actor::text,p_source_job_id::text
  ),0));
  SELECT value.*,job.status::text AS current_status
    INTO planned
  FROM memory.plan_owner_context_superseded_v1(100) AS value
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=actor AND job.job_id=value.source_job_id
  WHERE value.source_job_id=p_source_job_id
    AND value.source_evidence_id=p_source_evidence_id
    AND value.terminal_job_id=p_terminal_job_id
    AND value.terminal_evidence_id=p_terminal_evidence_id
    AND value.source_envelope_sha256=p_expected_source_envelope_sha256;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'context supersession target is absent or changed'
      USING ERRCODE='23514';
  END IF;
  prior_status:=planned.current_status;

  INSERT INTO memory.evidence_extraction_event(
    event_id,owner_user_id,job_id,operation_id,event_type,
    from_status,to_status,actor_type,actor_ref,details
  ) VALUES (
    new_event_id,actor,p_source_job_id,p_operation_id,'skipped',
    prior_status::memory.evidence_extraction_job_status,'skipped',
    'system','memory_v1_context_queue_reconciliation_v1',
    jsonb_build_object(
      'reason_code','superseded_by_terminal_source_envelope',
      'terminal_job_id',p_terminal_job_id,
      'terminal_evidence_id',p_terminal_evidence_id,
      'source_envelope_sha256',p_expected_source_envelope_sha256,
      'local_model_calls',0,'external_model_calls',0,
      'claims',0,'qdrant',0,'prompt_influence',0
    )
  );

  UPDATE memory.evidence_extraction_job
  SET status='skipped',lease_token=NULL,lease_expires_at=NULL,
      worker_id='memory_v1_context_queue_reconciliation_v1',
      last_error=NULL,
      result=jsonb_set(coalesce(result,'{}'::jsonb),'{final}',
        jsonb_build_object(
          'status','skipped',
          'reason_code','superseded_by_terminal_source_envelope',
          'terminal_job_id',p_terminal_job_id,
          'source_envelope_sha256',p_expected_source_envelope_sha256,
          'local_model_calls',0,'external_model_calls',0,
          'claims',0,'qdrant',0,'prompt_influence',0
        ),true)
  WHERE owner_user_id=actor AND job_id=p_source_job_id;

  INSERT INTO memory.evidence_context_queue_reconciliation_v1(
    reconciliation_id,owner_user_id,operation_id,source_job_id,
    source_evidence_id,terminal_job_id,terminal_evidence_id,
    source_envelope_sha256,reason_code
  ) VALUES (
    p_reconciliation_id,actor,p_operation_id,p_source_job_id,
    p_source_evidence_id,p_terminal_job_id,p_terminal_evidence_id,
    p_expected_source_envelope_sha256,
    'superseded_by_terminal_source_envelope'
  );

  RETURN QUERY SELECT p_source_job_id,p_terminal_job_id,
    'skipped'::text,'applied'::text,3;
END
$function$;

ALTER FUNCTION memory.plan_owner_context_superseded_v1(integer)
  OWNER TO memory_context_queue_reconciliation_maintainer;
ALTER FUNCTION memory.plan_owner_v5_local_orphan_v1(integer)
  OWNER TO memory_v5_local_inference_maintainer;
ALTER FUNCTION memory.finalize_owner_context_superseded_v1(
  uuid,uuid,uuid,uuid,uuid,uuid,text
) OWNER TO memory_context_queue_reconciliation_maintainer;
REVOKE ALL ON FUNCTION memory.plan_owner_context_superseded_v1(integer)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_local_orphan_v1(integer)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.finalize_owner_context_superseded_v1(
  uuid,uuid,uuid,uuid,uuid,uuid,text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.plan_owner_context_superseded_v1(integer)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_local_orphan_v1(integer)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.finalize_owner_context_superseded_v1(
  uuid,uuid,uuid,uuid,uuid,uuid,text
) TO brains_app;

COMMIT;
