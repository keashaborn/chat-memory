BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 governance migration must run as sage, current_user=%',
      current_user;
  END IF;
  IF to_regclass('memory.candidate') IS NULL
     OR to_regclass('memory.preference_candidate_review') IS NULL
     OR to_regclass('memory.project_knowledge_candidate_review') IS NULL THEN
    RAISE EXCEPTION 'memory V1 candidate and specialized review schemas are required';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brains_app') THEN
    RAISE EXCEPTION 'brains_app role is required';
  END IF;
END
$block$;

-- Only reviewed predicates enter the ontology. Cardinality drives the
-- deterministic contradiction gate in the governance worker.
INSERT INTO memory.predicate(predicate, object_kind, cardinality, description)
VALUES
  ('has_name', 'literal', 'one', 'User self-reported name'),
  ('spouse_name', 'literal', 'one', 'User self-reported spouse name'),
  ('has_children_names', 'literal', 'one', 'User self-reported child-name set'),
  ('spends_time_at', 'literal', 'many', 'User self-reported recurring place context'),
  ('financial_status', 'literal', 'one', 'User self-reported financial context'),
  ('goal', 'literal', 'many', 'User self-reported active goal'),
  ('often_takes_walks', 'literal', 'many', 'User self-reported recurring walking activity'),
  ('spent_time_in', 'literal', 'many', 'User self-reported prior location context'),
  ('did_activity_in_past', 'literal', 'many', 'User self-reported past activity')
ON CONFLICT (predicate) DO NOTHING;

DO $block$
BEGIN
  CREATE TYPE memory.governance_job_status AS ENUM (
    'pending', 'processing', 'completed', 'blocked', 'skipped', 'error'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END
$block$;

CREATE TABLE IF NOT EXISTS memory.governance_job (
  job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  lane text NOT NULL,
  action text NOT NULL,
  target_id uuid NOT NULL,
  target_hash text NOT NULL,
  status memory.governance_job_status NOT NULL DEFAULT 'pending',
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
  UNIQUE (owner_user_id, lane, action, target_id, target_hash),
  CHECK (lane IN ('claim', 'preference', 'project')),
  CHECK (action IN ('consolidate_candidate', 'recalculate_salience')),
  CHECK (target_hash ~ '^[0-9a-f]{64}$'),
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

CREATE INDEX IF NOT EXISTS governance_job_owner_ready_idx
  ON memory.governance_job(
    owner_user_id, status, available_at, priority, created_at, job_id
  )
  WHERE status IN ('pending', 'error', 'processing');

CREATE TABLE IF NOT EXISTS memory.governance_event (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  job_id uuid NOT NULL,
  event_type text NOT NULL,
  from_status memory.governance_job_status,
  to_status memory.governance_job_status NOT NULL,
  actor_type text NOT NULL,
  actor_ref text,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, event_id),
  FOREIGN KEY (owner_user_id, job_id)
    REFERENCES memory.governance_job(owner_user_id, job_id)
    ON DELETE RESTRICT,
  CHECK (btrim(event_type) <> ''),
  CHECK (actor_type IN ('review', 'worker', 'admin', 'system')),
  CHECK (actor_ref IS NULL OR btrim(actor_ref) <> ''),
  CHECK (jsonb_typeof(details) = 'object'),
  CHECK (pg_column_size(details) <= 16384)
);

CREATE INDEX IF NOT EXISTS governance_event_owner_job_time_idx
  ON memory.governance_event(owner_user_id, job_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory.governance_review_event (
  review_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  manifest_sha256 text NOT NULL,
  lane text NOT NULL,
  candidate_id uuid NOT NULL,
  candidate_hash text NOT NULL,
  decision text NOT NULL,
  reviewer_ref text NOT NULL,
  rationale text NOT NULL,
  reason_codes text[] NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (
    owner_user_id, manifest_sha256, lane, candidate_id, candidate_hash, decision
  ),
  CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),
  CHECK (lane IN ('claim', 'preference', 'project')),
  CHECK (decision IN ('approve', 'reject', 'accept', 'rewrite', 'defer', 'split')),
  CHECK (btrim(reviewer_ref) <> ''),
  CHECK (btrim(rationale) <> ''),
  CHECK (cardinality(reason_codes) > 0),
  CHECK (array_position(reason_codes, NULL) IS NULL),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS governance_review_owner_candidate_idx
  ON memory.governance_review_event(owner_user_id, lane, candidate_id, created_at DESC);

ALTER TABLE memory.governance_job OWNER TO sage;
ALTER TABLE memory.governance_event OWNER TO sage;
ALTER TABLE memory.governance_review_event OWNER TO sage;

CREATE OR REPLACE FUNCTION memory.guard_governance_job_update()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
BEGIN
  IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
     OR NEW.job_id IS DISTINCT FROM OLD.job_id
     OR NEW.lane IS DISTINCT FROM OLD.lane
     OR NEW.action IS DISTINCT FROM OLD.action
     OR NEW.target_id IS DISTINCT FROM OLD.target_id
     OR NEW.target_hash IS DISTINCT FROM OLD.target_hash
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'governance job identity is immutable'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.attempts < OLD.attempts THEN
    RAISE EXCEPTION 'governance attempts cannot decrease'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
    (OLD.status IN ('pending', 'error') AND NEW.status IN ('processing', 'skipped'))
    OR (OLD.status = 'processing' AND NEW.status IN (
      'completed', 'blocked', 'skipped', 'error'
    ))
  ) THEN
    RAISE EXCEPTION 'invalid governance transition: % -> %', OLD.status, NEW.status
      USING ERRCODE = '23514';
  END IF;
  NEW.updated_at = clock_timestamp();
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_governance_append_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
    USING ERRCODE = '42501';
END
$function$;

DROP TRIGGER IF EXISTS governance_job_update_guard ON memory.governance_job;
CREATE TRIGGER governance_job_update_guard
BEFORE UPDATE ON memory.governance_job
FOR EACH ROW EXECUTE FUNCTION memory.guard_governance_job_update();

DROP TRIGGER IF EXISTS governance_event_append_only_guard ON memory.governance_event;
CREATE TRIGGER governance_event_append_only_guard
BEFORE UPDATE OR DELETE ON memory.governance_event
FOR EACH ROW EXECUTE FUNCTION memory.guard_governance_append_only();

DROP TRIGGER IF EXISTS governance_review_append_only_guard
  ON memory.governance_review_event;
CREATE TRIGGER governance_review_append_only_guard
BEFORE UPDATE OR DELETE ON memory.governance_review_event
FOR EACH ROW EXECUTE FUNCTION memory.guard_governance_append_only();

CREATE OR REPLACE FUNCTION memory.queue_governance_job(
  p_owner_user_id uuid,
  p_lane text,
  p_action text,
  p_target_id uuid,
  p_target_hash text,
  p_actor_ref text,
  p_details jsonb DEFAULT '{}'::jsonb
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  v_job_id uuid;
BEGIN
  IF p_owner_user_id IS NULL
     OR p_owner_user_id = '00000000-0000-0000-0000-000000000000'::uuid THEN
    RAISE EXCEPTION 'governance owner must be a non-nil UUID';
  END IF;
  PERFORM set_config('app.user_id', p_owner_user_id::text, true);
  INSERT INTO memory.governance_job(
    owner_user_id, lane, action, target_id, target_hash
  ) VALUES (
    p_owner_user_id, p_lane, p_action, p_target_id, p_target_hash
  )
  ON CONFLICT (owner_user_id, lane, action, target_id, target_hash) DO NOTHING
  RETURNING job_id INTO v_job_id;

  IF v_job_id IS NOT NULL THEN
    INSERT INTO memory.governance_event(
      owner_user_id, job_id, event_type, from_status, to_status,
      actor_type, actor_ref, details
    ) VALUES (
      p_owner_user_id, v_job_id, 'queued', NULL, 'pending',
      'review', NULLIF(btrim(p_actor_ref), ''), COALESCE(p_details, '{}'::jsonb)
    );
  END IF;
  RETURN v_job_id;
END
$function$;

CREATE OR REPLACE FUNCTION memory.enqueue_claim_candidate_governance()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
  IF NEW.status = 'approved'::memory.candidate_status
     AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM NEW.status) THEN
    PERFORM memory.queue_governance_job(
      NEW.owner_user_id,
      'claim',
      'consolidate_candidate',
      NEW.candidate_id,
      NEW.proposal_hash,
      'claim_candidate_review',
      jsonb_build_object('from_status', OLD.status::text)
    );
  END IF;
  RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS candidate_enqueue_governance ON memory.candidate;
CREATE TRIGGER candidate_enqueue_governance
AFTER INSERT OR UPDATE ON memory.candidate
FOR EACH ROW EXECUTE FUNCTION memory.enqueue_claim_candidate_governance();

CREATE OR REPLACE FUNCTION memory.enqueue_preference_review_governance()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
  IF NEW.decision = 'accept' THEN
    PERFORM memory.queue_governance_job(
      NEW.owner_user_id,
      'preference',
      'consolidate_candidate',
      NEW.review_id,
      NEW.expected_candidate_hash,
      'preference_candidate_review',
      jsonb_build_object('candidate_id', NEW.candidate_id)
    );
  END IF;
  RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS preference_review_enqueue_governance
  ON memory.preference_candidate_review;
CREATE TRIGGER preference_review_enqueue_governance
AFTER INSERT ON memory.preference_candidate_review
FOR EACH ROW EXECUTE FUNCTION memory.enqueue_preference_review_governance();

CREATE OR REPLACE FUNCTION memory.enqueue_project_review_governance()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
  IF NEW.decision = 'accept' THEN
    PERFORM memory.queue_governance_job(
      NEW.owner_user_id,
      'project',
      'consolidate_candidate',
      NEW.review_id,
      NEW.expected_candidate_hash,
      'project_knowledge_candidate_review',
      jsonb_build_object(
        'candidate_id', NEW.candidate_id,
        'project_id', NEW.project_id
      )
    );
  END IF;
  RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS project_review_enqueue_governance
  ON memory.project_knowledge_candidate_review;
CREATE TRIGGER project_review_enqueue_governance
AFTER INSERT ON memory.project_knowledge_candidate_review
FOR EACH ROW EXECUTE FUNCTION memory.enqueue_project_review_governance();

CREATE OR REPLACE FUNCTION memory.enqueue_salience_governance(
  p_bucket date,
  p_actor_ref text DEFAULT 'memory_v1_governance_worker'
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $function$
DECLARE
  v_actor uuid;
  v_claim record;
  v_hash text;
  v_job_id uuid;
  v_count integer := 0;
BEGIN
  v_actor := memory.current_actor_user_id();
  IF v_actor IS NULL OR p_bucket IS NULL THEN
    RAISE EXCEPTION 'actor context and salience bucket are required'
      USING ERRCODE = '42501';
  END IF;
  FOR v_claim IN
    SELECT claim_id
    FROM memory.claim
    WHERE owner_user_id=v_actor
      AND status IN ('supported', 'uncertain', 'disputed')
    ORDER BY claim_id
  LOOP
    v_hash := encode(public.digest(
      'salience:v1:' || v_claim.claim_id::text || ':' || p_bucket::text,
      'sha256'
    ), 'hex');
    v_job_id := memory.queue_governance_job(
      v_actor,
      'claim',
      'recalculate_salience',
      v_claim.claim_id,
      v_hash,
      p_actor_ref,
      jsonb_build_object('bucket', p_bucket, 'algorithm', 'salience_v1')
    );
    IF v_job_id IS NOT NULL THEN
      v_count := v_count + 1;
    END IF;
  END LOOP;
  RETURN v_count;
END
$function$;

CREATE OR REPLACE FUNCTION memory.enqueue_claim_salience_governance(
  p_claim_id uuid,
  p_cause_hash text,
  p_actor_ref text DEFAULT 'memory_v1_governance_worker'
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $function$
DECLARE
  v_actor uuid;
  v_hash text;
BEGIN
  v_actor := memory.current_actor_user_id();
  IF v_actor IS NULL OR p_claim_id IS NULL
     OR p_cause_hash !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'actor, claim, and 64-character cause hash are required'
      USING ERRCODE = '22023';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM memory.claim
    WHERE owner_user_id=v_actor AND claim_id=p_claim_id
  ) THEN
    RAISE EXCEPTION 'claim is not visible to the actor'
      USING ERRCODE = 'P0002';
  END IF;
  v_hash := encode(public.digest(
    'salience:v1:cause:' || p_claim_id::text || ':' || p_cause_hash,
    'sha256'
  ), 'hex');
  RETURN memory.queue_governance_job(
    v_actor,
    'claim',
    'recalculate_salience',
    p_claim_id,
    v_hash,
    p_actor_ref,
    jsonb_build_object('cause_hash', p_cause_hash, 'algorithm', 'salience_v1')
  );
END
$function$;

DO $rls$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'governance_job', 'governance_event', 'governance_review_event'
  ]
  LOOP
    EXECUTE format('ALTER TABLE memory.%I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('ALTER TABLE memory.%I FORCE ROW LEVEL SECURITY', table_name);
    EXECUTE format('DROP POLICY IF EXISTS owner_isolation ON memory.%I', table_name);
    EXECUTE format(
      'CREATE POLICY owner_isolation ON memory.%I '
      'USING (owner_user_id = memory.current_actor_user_id()) '
      'WITH CHECK (owner_user_id = memory.current_actor_user_id())',
      table_name
    );
  END LOOP;
END
$rls$;

REVOKE ALL ON memory.governance_job,
              memory.governance_event,
              memory.governance_review_event
  FROM PUBLIC, brains_app;
GRANT SELECT, UPDATE ON memory.governance_job TO brains_app;
GRANT SELECT, INSERT ON memory.governance_event,
                        memory.governance_review_event TO brains_app;

ALTER FUNCTION memory.guard_governance_job_update() OWNER TO sage;
ALTER FUNCTION memory.guard_governance_append_only() OWNER TO sage;
ALTER FUNCTION memory.queue_governance_job(uuid,text,text,uuid,text,text,jsonb)
  OWNER TO sage;
ALTER FUNCTION memory.enqueue_claim_candidate_governance() OWNER TO sage;
ALTER FUNCTION memory.enqueue_preference_review_governance() OWNER TO sage;
ALTER FUNCTION memory.enqueue_project_review_governance() OWNER TO sage;
ALTER FUNCTION memory.enqueue_salience_governance(date,text) OWNER TO sage;
ALTER FUNCTION memory.enqueue_claim_salience_governance(uuid,text,text) OWNER TO sage;

REVOKE ALL ON FUNCTION memory.guard_governance_job_update() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.guard_governance_append_only() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.queue_governance_job(uuid,text,text,uuid,text,text,jsonb)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.enqueue_claim_candidate_governance() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.enqueue_preference_review_governance() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.enqueue_project_review_governance() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.enqueue_salience_governance(date,text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.enqueue_salience_governance(date,text)
  TO brains_app;
REVOKE ALL ON FUNCTION memory.enqueue_claim_salience_governance(uuid,text,text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.enqueue_claim_salience_governance(uuid,text,text)
  TO brains_app;

COMMIT;
