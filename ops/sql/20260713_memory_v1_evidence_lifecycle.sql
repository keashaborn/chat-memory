BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 evidence lifecycle migration must run as sage, current_user=%',
      current_user;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memory_evidence_maintainer') THEN
    CREATE ROLE memory_evidence_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$$;

ALTER ROLE memory_evidence_maintainer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

CREATE TABLE IF NOT EXISTS memory.evidence_lifecycle_event (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  request_id uuid NOT NULL,
  action text NOT NULL,
  outcome text NOT NULL,
  reason_code text NOT NULL,
  prior_status memory.record_status NOT NULL,
  resulting_status memory.record_status NOT NULL,
  content_sha256 text,
  content_was_present boolean NOT NULL,
  actor_user_id uuid NOT NULL,
  actor_type text NOT NULL,
  actor_ref text,
  invoked_by_role name NOT NULL,
  claim_link_count integer NOT NULL DEFAULT 0,
  candidate_link_count integer NOT NULL DEFAULT 0,
  artifact_link_count integer NOT NULL DEFAULT 0,
  source_copy_expected boolean NOT NULL DEFAULT false,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, event_id),
  UNIQUE (owner_user_id, request_id),
  CHECK (actor_user_id = owner_user_id),
  CHECK (action IN ('redact_content', 'delete_tombstone')),
  CHECK (outcome IN ('applied', 'already_redacted', 'already_deleted')),
  CHECK (
    reason_code IN (
      'user_request',
      'privacy_request',
      'correction',
      'source_retraction',
      'policy_violation',
      'retention_expiry',
      'administrative_repair'
    )
  ),
  CHECK (actor_type IN ('user', 'system', 'job', 'admin')),
  CHECK (actor_ref IS NULL OR length(actor_ref) <= 500),
  CHECK (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (claim_link_count >= 0),
  CHECK (candidate_link_count >= 0),
  CHECK (artifact_link_count >= 0),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS evidence_lifecycle_event_owner_evidence_time_idx
  ON memory.evidence_lifecycle_event(owner_user_id, evidence_id, created_at DESC);

ALTER TABLE memory.evidence_lifecycle_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.evidence_lifecycle_event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.evidence_lifecycle_event;
CREATE POLICY owner_isolation ON memory.evidence_lifecycle_event
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

CREATE OR REPLACE FUNCTION memory.guard_evidence_insert_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status NOT IN ('active', 'quarantined') THEN
      RAISE EXCEPTION 'new evidence must start active or quarantined'
        USING ERRCODE = '23514';
    END IF;
    IF NEW.content_sha256 IS NOT NULL
       AND NEW.content_sha256 !~ '^[0-9a-f]{64}$' THEN
      RAISE EXCEPTION 'evidence content_sha256 must be lowercase SHA-256 hex'
        USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
  END IF;

  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION
      'physical evidence deletion is disabled; use memory.transition_evidence_lifecycle()'
      USING ERRCODE = '42501';
  END IF;

  IF current_user <> 'memory_evidence_maintainer' THEN
    RAISE EXCEPTION
      'evidence rows are insert-only; use memory.transition_evidence_lifecycle()'
      USING ERRCODE = '42501';
  END IF;

  IF NEW.evidence_id IS DISTINCT FROM OLD.evidence_id
     OR NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
     OR NEW.kind IS DISTINCT FROM OLD.kind
     OR NEW.source_system IS DISTINCT FROM OLD.source_system
     OR NEW.external_id IS DISTINCT FROM OLD.external_id
     OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
     OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
     OR NEW.recorded_at IS DISTINCT FROM OLD.recorded_at
     OR NEW.directness IS DISTINCT FROM OLD.directness
     OR NEW.source_reliability IS DISTINCT FROM OLD.source_reliability
     OR NEW.independence_key IS DISTINCT FROM OLD.independence_key
     OR NEW.sensitivity IS DISTINCT FROM OLD.sensitivity
     OR NEW.metadata IS DISTINCT FROM OLD.metadata THEN
    RAISE EXCEPTION 'evidence identity, provenance, hash, and metadata are immutable'
      USING ERRCODE = '23514';
  END IF;

  IF NEW.content IS NOT NULL THEN
    RAISE EXCEPTION 'an evidence lifecycle transition may only clear content'
      USING ERRCODE = '23514';
  END IF;

  IF NOT (
    (OLD.status IN ('active', 'quarantined') AND NEW.status IN ('redacted', 'deleted'))
    OR (OLD.status = 'redacted' AND NEW.status = 'deleted')
    OR (
      OLD.status = NEW.status
      AND OLD.status IN ('redacted', 'deleted')
      AND OLD.content IS NOT NULL
    )
  ) THEN
    RAISE EXCEPTION 'invalid evidence lifecycle transition: % -> %',
      OLD.status, NEW.status
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS evidence_insert_only_guard ON memory.evidence;
CREATE TRIGGER evidence_insert_only_guard
BEFORE INSERT OR UPDATE OR DELETE ON memory.evidence
FOR EACH ROW EXECUTE FUNCTION memory.guard_evidence_insert_only();

CREATE OR REPLACE FUNCTION memory.guard_lifecycle_event_append_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
  RAISE EXCEPTION 'evidence lifecycle events are append-only'
    USING ERRCODE = '42501';
END
$$;

DROP TRIGGER IF EXISTS evidence_lifecycle_event_append_only_guard
  ON memory.evidence_lifecycle_event;
CREATE TRIGGER evidence_lifecycle_event_append_only_guard
BEFORE UPDATE OR DELETE ON memory.evidence_lifecycle_event
FOR EACH ROW EXECUTE FUNCTION memory.guard_lifecycle_event_append_only();

REVOKE ALL ON memory.evidence_lifecycle_event FROM PUBLIC;
REVOKE ALL ON memory.evidence_lifecycle_event FROM brains_app;
REVOKE UPDATE, DELETE ON memory.evidence FROM brains_app;
GRANT SELECT, INSERT ON memory.evidence TO brains_app;
GRANT SELECT ON memory.evidence_lifecycle_event TO brains_app;

GRANT USAGE ON SCHEMA memory TO memory_evidence_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_evidence_maintainer;
GRANT SELECT, UPDATE ON memory.evidence TO memory_evidence_maintainer;
GRANT SELECT, INSERT ON memory.evidence_lifecycle_event
  TO memory_evidence_maintainer;
GRANT SELECT
  ON memory.claim_evidence,
     memory.candidate,
     memory.artifact_occurrence,
     memory.artifact_endorsement
  TO memory_evidence_maintainer;

DO $$
BEGIN
  IF to_regprocedure('memory.guard_candidate_active_evidence()') IS NOT NULL THEN
    ALTER FUNCTION memory.guard_candidate_active_evidence() OWNER TO sage;
  END IF;
END
$$;

CREATE OR REPLACE FUNCTION memory.guard_candidate_active_evidence()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  evidence_status memory.record_status;
BEGIN
  IF memory.current_actor_user_id() IS NULL
     OR NEW.owner_user_id <> memory.current_actor_user_id() THEN
    RAISE EXCEPTION 'candidate owner does not match the current actor'
      USING ERRCODE = '42501';
  END IF;

  IF TG_OP = 'INSERT'
     OR NEW.evidence_id IS DISTINCT FROM OLD.evidence_id
     OR NEW.status IN ('approved', 'applied') THEN
    SELECT evidence.status
    INTO evidence_status
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id = NEW.owner_user_id
      AND evidence.evidence_id = NEW.evidence_id
    FOR KEY SHARE;

    IF NOT FOUND OR evidence_status <> 'active' THEN
      RAISE EXCEPTION 'candidate requires active evidence'
        USING ERRCODE = '23514';
    END IF;
  END IF;

  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS candidate_active_evidence_guard ON memory.candidate;
CREATE TRIGGER candidate_active_evidence_guard
BEFORE INSERT OR UPDATE OF evidence_id, status ON memory.candidate
FOR EACH ROW EXECUTE FUNCTION memory.guard_candidate_active_evidence();

DO $$
BEGIN
  IF to_regprocedure(
    'memory.transition_evidence_lifecycle(uuid,uuid,text,text,text,text,jsonb)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.transition_evidence_lifecycle(
      uuid, uuid, text, text, text, text, jsonb
    ) OWNER TO sage;
  END IF;
END
$$;

CREATE OR REPLACE FUNCTION memory.transition_evidence_lifecycle(
  p_evidence_id uuid,
  p_request_id uuid,
  p_action text,
  p_reason_code text,
  p_actor_type text DEFAULT 'user',
  p_actor_ref text DEFAULT NULL,
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS SETOF memory.evidence_lifecycle_event
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  v_actor uuid;
  v_evidence memory.evidence%ROWTYPE;
  v_existing memory.evidence_lifecycle_event%ROWTYPE;
  v_event memory.evidence_lifecycle_event%ROWTYPE;
  v_resulting_status memory.record_status;
  v_outcome text;
  v_claim_link_count integer;
  v_candidate_link_count integer;
  v_artifact_link_count integer;
BEGIN
  v_actor := memory.current_actor_user_id();
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_evidence_id IS NULL OR p_request_id IS NULL THEN
    RAISE EXCEPTION 'evidence_id and request_id are required'
      USING ERRCODE = '22023';
  END IF;
  IF p_action IS NULL
     OR p_action NOT IN ('redact_content', 'delete_tombstone') THEN
    RAISE EXCEPTION 'invalid evidence lifecycle action: %', p_action
      USING ERRCODE = '22023';
  END IF;
  IF p_reason_code IS NULL OR p_reason_code NOT IN (
    'user_request',
    'privacy_request',
    'correction',
    'source_retraction',
    'policy_violation',
    'retention_expiry',
    'administrative_repair'
  ) THEN
    RAISE EXCEPTION 'invalid evidence lifecycle reason_code: %', p_reason_code
      USING ERRCODE = '22023';
  END IF;
  IF p_actor_type IS NULL
     OR p_actor_type NOT IN ('user', 'system', 'job', 'admin') THEN
    RAISE EXCEPTION 'invalid evidence lifecycle actor_type: %', p_actor_type
      USING ERRCODE = '22023';
  END IF;
  IF p_actor_ref IS NOT NULL AND length(p_actor_ref) > 500 THEN
    RAISE EXCEPTION 'actor_ref exceeds 500 characters'
      USING ERRCODE = '22023';
  END IF;
  IF p_metadata IS NULL
     OR jsonb_typeof(p_metadata) <> 'object'
     OR pg_column_size(p_metadata) > 16384 THEN
    RAISE EXCEPTION 'metadata must be a JSON object no larger than 16 KiB'
      USING ERRCODE = '22023';
  END IF;

  PERFORM pg_advisory_xact_lock(
    hashtextextended(v_actor::text || ':' || p_request_id::text, 0)
  );

  SELECT event.*
  INTO v_existing
  FROM memory.evidence_lifecycle_event AS event
  WHERE event.owner_user_id = v_actor
    AND event.request_id = p_request_id;

  IF FOUND THEN
    IF v_existing.evidence_id <> p_evidence_id
       OR v_existing.action <> p_action
       OR v_existing.reason_code <> p_reason_code
       OR v_existing.actor_type <> p_actor_type
       OR v_existing.actor_ref IS DISTINCT FROM p_actor_ref
       OR v_existing.metadata <> p_metadata THEN
      RAISE EXCEPTION 'request_id was already used with different lifecycle inputs'
        USING ERRCODE = '22023';
    END IF;
    RETURN NEXT v_existing;
    RETURN;
  END IF;

  SELECT evidence.*
  INTO v_evidence
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id = v_actor
    AND evidence.evidence_id = p_evidence_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'evidence is not visible to the current actor'
      USING ERRCODE = 'P0002';
  END IF;

  IF p_action = 'redact_content' THEN
    IF v_evidence.status = 'deleted' THEN
      v_resulting_status := 'deleted';
      v_outcome := CASE
        WHEN v_evidence.content IS NULL THEN 'already_deleted'
        ELSE 'applied'
      END;
    ELSIF v_evidence.status = 'redacted' AND v_evidence.content IS NULL THEN
      v_resulting_status := 'redacted';
      v_outcome := 'already_redacted';
    ELSE
      v_resulting_status := 'redacted';
      v_outcome := 'applied';
    END IF;
  ELSE
    v_resulting_status := 'deleted';
    IF v_evidence.status = 'deleted' AND v_evidence.content IS NULL THEN
      v_outcome := 'already_deleted';
    ELSE
      v_outcome := 'applied';
    END IF;
  END IF;

  SELECT count(*)::integer
  INTO v_claim_link_count
  FROM memory.claim_evidence AS link
  WHERE link.owner_user_id = v_actor
    AND link.evidence_id = p_evidence_id;

  SELECT count(*)::integer
  INTO v_candidate_link_count
  FROM memory.candidate AS candidate
  WHERE candidate.owner_user_id = v_actor
    AND candidate.evidence_id = p_evidence_id;

  SELECT (
    (SELECT count(*) FROM memory.artifact_occurrence AS occurrence
     WHERE occurrence.owner_user_id = v_actor
       AND occurrence.evidence_id = p_evidence_id)
    +
    (SELECT count(*) FROM memory.artifact_endorsement AS endorsement
     WHERE endorsement.owner_user_id = v_actor
       AND endorsement.evidence_id = p_evidence_id)
  )::integer
  INTO v_artifact_link_count;

  IF v_outcome = 'applied' THEN
    UPDATE memory.evidence
    SET content = NULL,
        status = v_resulting_status
    WHERE owner_user_id = v_actor
      AND evidence_id = p_evidence_id;
  END IF;

  INSERT INTO memory.evidence_lifecycle_event(
    owner_user_id,
    evidence_id,
    request_id,
    action,
    outcome,
    reason_code,
    prior_status,
    resulting_status,
    content_sha256,
    content_was_present,
    actor_user_id,
    actor_type,
    actor_ref,
    invoked_by_role,
    claim_link_count,
    candidate_link_count,
    artifact_link_count,
    source_copy_expected,
    metadata
  ) VALUES (
    v_actor,
    p_evidence_id,
    p_request_id,
    p_action,
    v_outcome,
    p_reason_code,
    v_evidence.status,
    v_resulting_status,
    v_evidence.content_sha256,
    v_evidence.content IS NOT NULL,
    v_actor,
    p_actor_type,
    p_actor_ref,
    session_user,
    v_claim_link_count,
    v_candidate_link_count,
    v_artifact_link_count,
    v_evidence.source_system = 'public.chat_log',
    p_metadata
  )
  RETURNING * INTO v_event;

  RETURN NEXT v_event;
END
$$;

REVOKE ALL ON FUNCTION memory.guard_candidate_active_evidence() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.transition_evidence_lifecycle(
  uuid, uuid, text, text, text, text, jsonb
) FROM PUBLIC;

GRANT CREATE ON SCHEMA memory TO memory_evidence_maintainer;
ALTER FUNCTION memory.guard_candidate_active_evidence()
  OWNER TO memory_evidence_maintainer;
ALTER FUNCTION memory.transition_evidence_lifecycle(
  uuid, uuid, text, text, text, text, jsonb
) OWNER TO memory_evidence_maintainer;
REVOKE CREATE ON SCHEMA memory FROM memory_evidence_maintainer;

GRANT EXECUTE ON FUNCTION memory.transition_evidence_lifecycle(
  uuid, uuid, text, text, text, text, jsonb
) TO brains_app;

COMMIT;
