BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'evidence extraction queue migration requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regrole('memory_intake_maintainer') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.evidence_intake_terminal') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_evidence_intake_v1(text,integer,uuid)'
     ) IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL THEN
    RAISE EXCEPTION 'evidence extraction queue prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_extraction_queue_maintainer') IS NULL THEN
    CREATE ROLE memory_extraction_queue_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$role$;

ALTER ROLE memory_extraction_queue_maintainer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

DO $status_type$
BEGIN
  CREATE TYPE memory.evidence_extraction_job_status AS ENUM (
    'pending',
    'processing',
    'review_required',
    'completed',
    'skipped',
    'error'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END
$status_type$;

ALTER TABLE memory.evidence_intake_terminal
  DROP CONSTRAINT evidence_intake_terminal_outcome_check;
ALTER TABLE memory.evidence_intake_terminal
  ADD CONSTRAINT evidence_intake_terminal_outcome_check
  CHECK (outcome IN ('empty','skipped','dispatched'));

ALTER TABLE memory.evidence_intake_terminal
  DROP CONSTRAINT evidence_intake_terminal_reason_code_check;
ALTER TABLE memory.evidence_intake_terminal
  ADD CONSTRAINT evidence_intake_terminal_reason_code_check
  CHECK (
    reason_code IN (
      'empty_content',
      'missing_content_hash',
      'upstream_completed_empty',
      'upstream_skipped',
      'upstream_review_required',
      'upstream_link_inconsistency',
      'eligible_dispatched'
    )
  );

CREATE TABLE IF NOT EXISTS memory.evidence_extraction_job (
  job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  intake_terminal_id uuid NOT NULL,
  selector_version text NOT NULL,
  evidence_content_sha256 text NOT NULL,
  route text NOT NULL,
  intake_reason_code text NOT NULL,
  status memory.evidence_extraction_job_status NOT NULL DEFAULT 'pending',
  priority smallint NOT NULL DEFAULT 100,
  attempts integer NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  lease_token uuid,
  lease_expires_at timestamptz,
  worker_id text,
  last_error text,
  result jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,job_id),
  UNIQUE (owner_user_id,evidence_id,selector_version),
  UNIQUE (owner_user_id,intake_terminal_id),
  FOREIGN KEY (owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,intake_terminal_id)
    REFERENCES memory.evidence_intake_terminal(owner_user_id,terminal_id)
    ON DELETE RESTRICT,
  CHECK (selector_version ~ '^[a-z0-9][a-z0-9_.-]{2,63}$'),
  CHECK (evidence_content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (
    route IN (
      'relational_extraction',
      'artifact_assessment',
      'structured_projection'
    )
  ),
  CHECK (intake_reason_code='eligible_unprocessed'),
  CHECK (priority BETWEEN 0 AND 1000),
  CHECK (attempts>=0),
  CHECK (worker_id IS NULL OR btrim(worker_id)<>''),
  CHECK (last_error IS NULL OR length(last_error)<=2000),
  CHECK (jsonb_typeof(result)='object'),
  CHECK (pg_column_size(result)<=32768),
  CHECK (
    (
      status='processing'
      AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL
    )
    OR
    (
      status<>'processing'
      AND lease_token IS NULL
      AND lease_expires_at IS NULL
    )
  )
);

CREATE INDEX IF NOT EXISTS evidence_extraction_job_owner_ready_idx
  ON memory.evidence_extraction_job(
    owner_user_id,status,available_at,priority,created_at,job_id
  )
  WHERE status IN ('pending','error','processing');
CREATE INDEX IF NOT EXISTS evidence_extraction_job_owner_evidence_idx
  ON memory.evidence_extraction_job(owner_user_id,evidence_id);

CREATE TABLE IF NOT EXISTS memory.evidence_extraction_event (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  job_id uuid NOT NULL,
  event_type text NOT NULL,
  from_status memory.evidence_extraction_job_status,
  to_status memory.evidence_extraction_job_status NOT NULL,
  actor_type text NOT NULL,
  actor_ref text,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,event_id),
  FOREIGN KEY (owner_user_id,job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id,job_id)
    ON DELETE RESTRICT,
  CHECK (
    event_type IN (
      'queued',
      'claimed',
      'checkpointed',
      'review_required',
      'completed',
      'skipped',
      'error'
    )
  ),
  CHECK (actor_type IN ('system','worker','admin')),
  CHECK (actor_ref IS NULL OR length(actor_ref)<=500),
  CHECK (jsonb_typeof(details)='object'),
  CHECK (pg_column_size(details)<=16384)
);

CREATE INDEX IF NOT EXISTS evidence_extraction_event_owner_job_time_idx
  ON memory.evidence_extraction_event(
    owner_user_id,job_id,created_at,event_id
  );

ALTER TABLE memory.evidence_extraction_job OWNER TO sage;
ALTER TABLE memory.evidence_extraction_event OWNER TO sage;

CREATE OR REPLACE FUNCTION memory.guard_evidence_extraction_job_update()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=pg_catalog
AS $function$
BEGIN
  IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
     OR NEW.job_id IS DISTINCT FROM OLD.job_id
     OR NEW.evidence_id IS DISTINCT FROM OLD.evidence_id
     OR NEW.intake_terminal_id IS DISTINCT FROM OLD.intake_terminal_id
     OR NEW.selector_version IS DISTINCT FROM OLD.selector_version
     OR NEW.evidence_content_sha256
        IS DISTINCT FROM OLD.evidence_content_sha256
     OR NEW.route IS DISTINCT FROM OLD.route
     OR NEW.intake_reason_code IS DISTINCT FROM OLD.intake_reason_code
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'evidence extraction job identity is immutable'
      USING ERRCODE='23514';
  END IF;
  IF NEW.attempts<OLD.attempts THEN
    RAISE EXCEPTION 'evidence extraction attempts cannot decrease'
      USING ERRCODE='23514';
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
    (OLD.status='pending' AND NEW.status IN ('processing','skipped'))
    OR (
      OLD.status='processing'
      AND NEW.status IN (
        'processing','review_required','completed','skipped','error'
      )
    )
    OR (OLD.status='error' AND NEW.status IN ('processing','skipped'))
    OR (
      OLD.status='review_required'
      AND NEW.status IN ('completed','skipped')
    )
  ) THEN
    RAISE EXCEPTION 'invalid evidence extraction transition: % -> %',
      OLD.status,NEW.status
      USING ERRCODE='23514';
  END IF;
  NEW.updated_at=clock_timestamp();
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_evidence_extraction_event_append_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'memory.evidence_extraction_event is append-only'
    USING ERRCODE='42501';
END
$function$;

DROP TRIGGER IF EXISTS evidence_extraction_job_update_guard
  ON memory.evidence_extraction_job;
CREATE TRIGGER evidence_extraction_job_update_guard
BEFORE UPDATE ON memory.evidence_extraction_job
FOR EACH ROW
EXECUTE FUNCTION memory.guard_evidence_extraction_job_update();

DROP TRIGGER IF EXISTS evidence_extraction_event_append_only_guard
  ON memory.evidence_extraction_event;
CREATE TRIGGER evidence_extraction_event_append_only_guard
BEFORE UPDATE OR DELETE ON memory.evidence_extraction_event
FOR EACH ROW
EXECUTE FUNCTION memory.guard_evidence_extraction_event_append_only();

ALTER TABLE memory.evidence_extraction_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.evidence_extraction_job FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.evidence_extraction_job;
CREATE POLICY owner_isolation ON memory.evidence_extraction_job
  USING (owner_user_id=(SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()));

ALTER TABLE memory.evidence_extraction_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.evidence_extraction_event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.evidence_extraction_event;
CREATE POLICY owner_isolation ON memory.evidence_extraction_event
  USING (owner_user_id=(SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()));

DO $function_ownership$
BEGIN
  IF to_regprocedure(
    'memory.enqueue_owner_evidence_extraction_v1(uuid,text,text,text,text)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.enqueue_owner_evidence_extraction_v1(
      uuid,text,text,text,text
    ) OWNER TO sage;
  END IF;
END
$function_ownership$;

CREATE OR REPLACE FUNCTION memory.enqueue_owner_evidence_extraction_v1(
  p_evidence_id uuid,
  p_selector_version text,
  p_expected_content_sha256 text,
  p_expected_route text,
  p_expected_reason_code text
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
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  planned record;
  existing_job memory.evidence_extraction_job%ROWTYPE;
  existing_terminal memory.evidence_intake_terminal%ROWTYPE;
  job_found boolean;
  terminal_found boolean;
  new_job_id uuid := gen_random_uuid();
  new_terminal_id uuid := gen_random_uuid();
  fingerprint text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'evidence extraction enqueue requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_evidence_id IS NULL
     OR p_selector_version IS NULL
     OR p_selector_version !~ '^[a-z0-9][a-z0-9_.-]{2,63}$'
     OR p_expected_content_sha256 IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_route NOT IN (
       'relational_extraction',
       'artifact_assessment',
       'structured_projection'
     )
     OR p_expected_reason_code<>'eligible_unprocessed' THEN
    RAISE EXCEPTION 'evidence extraction enqueue inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|',actor::text,p_evidence_id::text,p_selector_version),
    0
  ));

  SELECT extraction_job.* INTO existing_job
  FROM memory.evidence_extraction_job AS extraction_job
  WHERE extraction_job.owner_user_id=actor
    AND extraction_job.evidence_id=p_evidence_id
    AND extraction_job.selector_version=p_selector_version;
  job_found := FOUND;

  SELECT terminal.* INTO existing_terminal
  FROM memory.evidence_intake_terminal AS terminal
  WHERE terminal.owner_user_id=actor
    AND terminal.evidence_id=p_evidence_id
    AND terminal.selector_version=p_selector_version;
  terminal_found := FOUND;

  IF job_found OR terminal_found THEN
    IF NOT job_found
       OR NOT terminal_found
       OR existing_job.intake_terminal_id<>existing_terminal.terminal_id
       OR existing_job.evidence_content_sha256
          IS DISTINCT FROM p_expected_content_sha256
       OR existing_terminal.evidence_content_sha256
          IS DISTINCT FROM p_expected_content_sha256
       OR existing_job.route IS DISTINCT FROM p_expected_route
       OR existing_job.intake_reason_code
          IS DISTINCT FROM p_expected_reason_code
       OR existing_terminal.outcome<>'dispatched'
       OR existing_terminal.reason_code<>'eligible_dispatched' THEN
      RAISE EXCEPTION 'evidence extraction enqueue replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      existing_job.job_id,
      existing_terminal.terminal_id,
      existing_job.status::text,
      'replayed'::text;
    RETURN;
  END IF;

  SELECT * INTO planned
  FROM memory.plan_owner_evidence_intake_v1(
    p_selector_version,1,p_evidence_id
  );
  IF NOT FOUND
     OR planned.outcome<>'eligible'
     OR planned.evidence_content_sha256
        IS DISTINCT FROM p_expected_content_sha256
     OR planned.route IS DISTINCT FROM p_expected_route
     OR planned.reason_code IS DISTINCT FROM p_expected_reason_code THEN
    RAISE EXCEPTION 'evidence extraction enqueue plan changed'
      USING ERRCODE='23514';
  END IF;

  fingerprint := encode(
    public.digest(
      convert_to(
        jsonb_build_object(
          'owner_user_id',actor,
          'evidence_id',p_evidence_id,
          'selector_version',p_selector_version,
          'evidence_content_sha256',p_expected_content_sha256,
          'outcome','dispatched',
          'reason_code','eligible_dispatched',
          'route',p_expected_route
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
    source_job_id,
    source_job_status,
    source_job_pipeline_version,
    decision_fingerprint,
    actor_user_id,
    invoked_by_role,
    details
  ) VALUES (
    new_terminal_id,
    actor,
    p_evidence_id,
    p_selector_version,
    'dispatched',
    'eligible_dispatched',
    p_expected_content_sha256,
    NULL,
    NULL,
    NULL,
    fingerprint,
    actor,
    session_user,
    jsonb_build_object(
      'route',p_expected_route,
      'extraction_job_id',new_job_id,
      'plan_reason_code',p_expected_reason_code
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
    new_job_id,
    actor,
    p_evidence_id,
    new_terminal_id,
    p_selector_version,
    p_expected_content_sha256,
    p_expected_route,
    p_expected_reason_code,
    'pending'
  );

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,
    job_id,
    event_type,
    from_status,
    to_status,
    actor_type,
    actor_ref,
    details
  ) VALUES (
    actor,
    new_job_id,
    'queued',
    NULL,
    'pending',
    'system',
    'evidence_intake_dispatcher',
    jsonb_build_object(
      'intake_terminal_id',new_terminal_id,
      'selector_version',p_selector_version,
      'route',p_expected_route
    )
  );

  RETURN QUERY SELECT
    new_job_id,
    new_terminal_id,
    'pending'::text,
    'applied'::text;
END
$function$;

REVOKE ALL ON
  memory.evidence_extraction_job,
  memory.evidence_extraction_event
FROM PUBLIC,brains_app;
GRANT SELECT ON
  memory.evidence_extraction_job,
  memory.evidence_extraction_event
TO brains_app;

GRANT USAGE ON SCHEMA memory
  TO memory_extraction_queue_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_extraction_queue_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_evidence_intake_v1(text,integer,uuid)
  TO memory_extraction_queue_maintainer;
GRANT SELECT,INSERT ON
  memory.evidence_extraction_job,
  memory.evidence_extraction_event
TO memory_extraction_queue_maintainer;
GRANT SELECT,INSERT ON memory.evidence_intake_terminal
  TO memory_extraction_queue_maintainer;

ALTER FUNCTION memory.enqueue_owner_evidence_extraction_v1(
  uuid,text,text,text,text
) OWNER TO memory_extraction_queue_maintainer;

REVOKE ALL ON FUNCTION
  memory.guard_evidence_extraction_job_update()
  FROM PUBLIC,brains_app,memory_extraction_queue_maintainer;
REVOKE ALL ON FUNCTION
  memory.guard_evidence_extraction_event_append_only()
  FROM PUBLIC,brains_app,memory_extraction_queue_maintainer;
REVOKE ALL ON FUNCTION
  memory.enqueue_owner_evidence_extraction_v1(
    uuid,text,text,text,text
  )
  FROM PUBLIC,brains_app,memory_extraction_queue_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.enqueue_owner_evidence_extraction_v1(
    uuid,text,text,text,text
  )
  TO brains_app;

COMMIT;
