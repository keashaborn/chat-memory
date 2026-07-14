BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 consolidation migration must run as sage, current_user=%',
      current_user;
  END IF;
  IF to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.candidate') IS NULL THEN
    RAISE EXCEPTION 'memory V1 foundation is required';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brains_app') THEN
    RAISE EXCEPTION 'brains_app role is required';
  END IF;
END
$block$;

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

DO $block$
BEGIN
  CREATE TYPE memory.consolidation_job_status AS ENUM (
    'pending',
    'processing',
    'review_required',
    'completed',
    'skipped',
    'error'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END
$block$;

CREATE TABLE IF NOT EXISTS memory.consolidation_job (
  job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  source_system text NOT NULL,
  source_external_id text NOT NULL,
  source_sha256 text NOT NULL,
  source_recorded_at timestamptz NOT NULL,
  status memory.consolidation_job_status NOT NULL DEFAULT 'pending',
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
  UNIQUE (owner_user_id, job_id),
  UNIQUE (owner_user_id, source_system, source_external_id),
  CHECK (btrim(source_system) <> ''),
  CHECK (btrim(source_external_id) <> ''),
  CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (priority BETWEEN 0 AND 1000),
  CHECK (attempts >= 0),
  CHECK (worker_id IS NULL OR btrim(worker_id) <> ''),
  CHECK (last_error IS NULL OR length(last_error) <= 2000),
  CHECK (jsonb_typeof(result) = 'object'),
  CHECK (pg_column_size(result) <= 32768),
  CHECK (
    (status = 'processing' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    OR
    (status <> 'processing' AND lease_token IS NULL AND lease_expires_at IS NULL)
  )
);

CREATE INDEX IF NOT EXISTS consolidation_job_owner_ready_idx
  ON memory.consolidation_job(
    owner_user_id, status, available_at, priority, source_recorded_at, job_id
  )
  WHERE status IN ('pending', 'error', 'processing');

CREATE TABLE IF NOT EXISTS memory.consolidation_event (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  job_id uuid NOT NULL,
  event_type text NOT NULL,
  from_status memory.consolidation_job_status,
  to_status memory.consolidation_job_status NOT NULL,
  actor_type text NOT NULL,
  actor_ref text,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, event_id),
  FOREIGN KEY (owner_user_id, job_id)
    REFERENCES memory.consolidation_job(owner_user_id, job_id)
    ON DELETE RESTRICT,
  CHECK (btrim(event_type) <> ''),
  CHECK (actor_type IN ('capture', 'worker', 'admin', 'system')),
  CHECK (actor_ref IS NULL OR btrim(actor_ref) <> ''),
  CHECK (jsonb_typeof(details) = 'object'),
  CHECK (pg_column_size(details) <= 16384)
);

CREATE INDEX IF NOT EXISTS consolidation_event_owner_job_time_idx
  ON memory.consolidation_event(owner_user_id, job_id, created_at DESC);

ALTER TABLE memory.consolidation_job OWNER TO sage;
ALTER TABLE memory.consolidation_event OWNER TO sage;

CREATE OR REPLACE FUNCTION memory.guard_consolidation_job_update()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
BEGIN
  IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
     OR NEW.job_id IS DISTINCT FROM OLD.job_id
     OR NEW.source_system IS DISTINCT FROM OLD.source_system
     OR NEW.source_external_id IS DISTINCT FROM OLD.source_external_id
     OR NEW.source_sha256 IS DISTINCT FROM OLD.source_sha256
     OR NEW.source_recorded_at IS DISTINCT FROM OLD.source_recorded_at
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'consolidation job source identity is immutable'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.attempts < OLD.attempts THEN
    RAISE EXCEPTION 'consolidation attempts cannot decrease'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
    (OLD.status = 'pending' AND NEW.status IN ('processing', 'skipped'))
    OR (OLD.status = 'processing' AND NEW.status IN (
      'processing', 'review_required', 'completed', 'skipped', 'error'
    ))
    OR (OLD.status = 'error' AND NEW.status IN ('processing', 'skipped'))
    OR (OLD.status = 'review_required' AND NEW.status IN ('completed', 'skipped'))
  ) THEN
    RAISE EXCEPTION 'invalid consolidation transition: % -> %', OLD.status, NEW.status
      USING ERRCODE = '23514';
  END IF;
  NEW.updated_at = clock_timestamp();
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_consolidation_event_append_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'memory.consolidation_event is append-only'
    USING ERRCODE = '42501';
END
$function$;

DROP TRIGGER IF EXISTS consolidation_job_update_guard
  ON memory.consolidation_job;
CREATE TRIGGER consolidation_job_update_guard
BEFORE UPDATE ON memory.consolidation_job
FOR EACH ROW EXECUTE FUNCTION memory.guard_consolidation_job_update();

DROP TRIGGER IF EXISTS consolidation_event_append_only_guard
  ON memory.consolidation_event;
CREATE TRIGGER consolidation_event_append_only_guard
BEFORE UPDATE OR DELETE ON memory.consolidation_event
FOR EACH ROW EXECUTE FUNCTION memory.guard_consolidation_event_append_only();

-- Existing authenticated raw turns enter the queue without model calls or
-- governed-memory writes. Non-UUID legacy aliases have owner_user_id NULL and
-- are deliberately excluded.
INSERT INTO memory.consolidation_job(
  owner_user_id,
  source_system,
  source_external_id,
  source_sha256,
  source_recorded_at,
  status,
  result
)
SELECT
  log.owner_user_id,
  'public.chat_log',
  log.id::text,
  encode(digest(COALESCE(log.text, ''), 'sha256'), 'hex'),
  COALESCE(log.created_at, clock_timestamp()),
  'pending'::memory.consolidation_job_status,
  jsonb_build_object('capture', 'migration_backfill')
FROM public.chat_log AS log
WHERE log.owner_user_id IS NOT NULL
  AND log.source = 'frontend/chat:user'
ON CONFLICT (owner_user_id, source_system, source_external_id) DO NOTHING;

INSERT INTO memory.consolidation_event(
  owner_user_id,
  job_id,
  event_type,
  from_status,
  to_status,
  actor_type,
  actor_ref,
  details
)
SELECT
  job.owner_user_id,
  job.job_id,
  'queued',
  NULL,
  job.status,
  'system',
  'migration_backfill',
  '{}'::jsonb
FROM memory.consolidation_job AS job
WHERE NOT EXISTS (
  SELECT 1
  FROM memory.consolidation_event AS event
  WHERE event.owner_user_id=job.owner_user_id
    AND event.job_id=job.job_id
    AND event.event_type='queued'
);

CREATE OR REPLACE FUNCTION memory.enqueue_chat_log_consolidation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
  queued_job_id uuid;
BEGIN
  IF NEW.owner_user_id IS NULL OR NEW.source <> 'frontend/chat:user' THEN
    RETURN NEW;
  END IF;

  PERFORM set_config('app.user_id', NEW.owner_user_id::text, true);
  INSERT INTO memory.consolidation_job(
    owner_user_id,
    source_system,
    source_external_id,
    source_sha256,
    source_recorded_at,
    status,
    result
  ) VALUES (
    NEW.owner_user_id,
    'public.chat_log',
    NEW.id::text,
    encode(digest(COALESCE(NEW.text, ''), 'sha256'), 'hex'),
    COALESCE(NEW.created_at, clock_timestamp()),
    'pending',
    jsonb_build_object('capture', 'chat_log_trigger')
  )
  ON CONFLICT (owner_user_id, source_system, source_external_id) DO NOTHING
  RETURNING job_id INTO queued_job_id;

  IF queued_job_id IS NOT NULL THEN
    INSERT INTO memory.consolidation_event(
      owner_user_id, job_id, event_type, from_status, to_status,
      actor_type, actor_ref, details
    ) VALUES (
      NEW.owner_user_id, queued_job_id, 'queued', NULL, 'pending',
      'capture', 'public.chat_log', '{}'::jsonb
    );
  END IF;
  RETURN NEW;
END
$function$;

ALTER FUNCTION memory.enqueue_chat_log_consolidation() OWNER TO sage;
REVOKE ALL ON FUNCTION memory.enqueue_chat_log_consolidation() FROM PUBLIC;

DROP TRIGGER IF EXISTS chat_log_enqueue_memory_v1_consolidation
  ON public.chat_log;
CREATE TRIGGER chat_log_enqueue_memory_v1_consolidation
AFTER INSERT ON public.chat_log
FOR EACH ROW
WHEN (NEW.owner_user_id IS NOT NULL AND NEW.source = 'frontend/chat:user')
EXECUTE FUNCTION memory.enqueue_chat_log_consolidation();

ALTER TABLE memory.consolidation_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.consolidation_job FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.consolidation_job;
CREATE POLICY owner_isolation ON memory.consolidation_job
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

ALTER TABLE memory.consolidation_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.consolidation_event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.consolidation_event;
CREATE POLICY owner_isolation ON memory.consolidation_event
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

REVOKE ALL ON memory.consolidation_job, memory.consolidation_event
  FROM PUBLIC, brains_app;
GRANT USAGE ON SCHEMA memory TO brains_app;
GRANT SELECT, UPDATE ON memory.consolidation_job TO brains_app;
GRANT SELECT, INSERT ON memory.consolidation_event TO brains_app;

REVOKE ALL ON FUNCTION memory.guard_consolidation_job_update() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.guard_consolidation_event_append_only() FROM PUBLIC;

COMMIT;
