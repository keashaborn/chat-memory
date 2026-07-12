BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 migration must run as sage, current_user=%', current_user;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brains_app') THEN
    CREATE ROLE brains_app
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS memory AUTHORIZATION sage;
ALTER SCHEMA memory OWNER TO sage;

DO $$ BEGIN
  CREATE TYPE memory.evidence_kind AS ENUM (
    'user_statement',
    'system_event',
    'structured_measurement',
    'derived_result',
    'document',
    'external_observation'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE memory.record_status AS ENUM (
    'active', 'quarantined', 'redacted', 'deleted'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE memory.claim_status AS ENUM (
    'candidate',
    'supported',
    'uncertain',
    'disputed',
    'superseded',
    'retracted',
    'quarantined'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE memory.evidence_stance AS ENUM (
    'supports', 'opposes', 'qualifies', 'context'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE memory.claim_relation_type AS ENUM (
    'supersedes', 'contradicts', 'qualifies', 'depends_on', 'derived_from'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE memory.sensitivity_level AS ENUM (
    'low', 'medium', 'high', 'restricted'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE memory.candidate_status AS ENUM (
    'proposed', 'review_required', 'approved', 'rejected', 'applied'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE memory.outbox_status AS ENUM (
    'pending', 'processing', 'done', 'error'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE OR REPLACE FUNCTION memory.current_actor_user_id()
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
  SELECT NULLIF(current_setting('app.user_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION memory.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END
$$;

CREATE TABLE IF NOT EXISTS memory.predicate (
  predicate text PRIMARY KEY,
  object_kind text NOT NULL DEFAULT 'either'
    CHECK (object_kind IN ('entity', 'literal', 'either')),
  cardinality text NOT NULL DEFAULT 'many'
    CHECK (cardinality IN ('one', 'many')),
  description text,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory.entity (
  entity_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  entity_key text NOT NULL,
  entity_type text NOT NULL,
  canonical_name text NOT NULL,
  normalized_name text NOT NULL,
  status memory.record_status NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, entity_key),
  UNIQUE (owner_user_id, entity_id),
  CHECK (btrim(entity_key) <> ''),
  CHECK (btrim(entity_type) <> ''),
  CHECK (btrim(canonical_name) <> ''),
  CHECK (btrim(normalized_name) <> '')
);

CREATE INDEX IF NOT EXISTS entity_owner_name_idx
  ON memory.entity(owner_user_id, entity_type, normalized_name)
  WHERE status = 'active';

CREATE TABLE IF NOT EXISTS memory.evidence (
  evidence_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  kind memory.evidence_kind NOT NULL,
  source_system text NOT NULL,
  external_id text NOT NULL,
  content text,
  content_sha256 text,
  observed_at timestamptz,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  directness numeric(4,3),
  source_reliability numeric(4,3),
  independence_key text,
  sensitivity memory.sensitivity_level NOT NULL DEFAULT 'medium',
  status memory.record_status NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (owner_user_id, source_system, external_id),
  UNIQUE (owner_user_id, evidence_id),
  CHECK (btrim(source_system) <> ''),
  CHECK (btrim(external_id) <> ''),
  CHECK (directness IS NULL OR directness BETWEEN 0 AND 1),
  CHECK (source_reliability IS NULL OR source_reliability BETWEEN 0 AND 1)
);

CREATE INDEX IF NOT EXISTS evidence_owner_time_idx
  ON memory.evidence(owner_user_id, observed_at DESC, recorded_at DESC)
  WHERE status = 'active';

CREATE TABLE IF NOT EXISTS memory.entity_alias (
  owner_user_id uuid NOT NULL,
  entity_id uuid NOT NULL,
  alias text NOT NULL,
  normalized_alias text NOT NULL,
  alias_type text NOT NULL DEFAULT 'observed',
  evidence_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, entity_id, normalized_alias),
  FOREIGN KEY (owner_user_id, entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id)
    ON DELETE CASCADE,
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE SET NULL,
  CHECK (btrim(alias) <> ''),
  CHECK (btrim(normalized_alias) <> '')
);

CREATE INDEX IF NOT EXISTS entity_alias_lookup_idx
  ON memory.entity_alias(owner_user_id, normalized_alias);

CREATE TABLE IF NOT EXISTS memory.claim (
  claim_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  subject_entity_id uuid NOT NULL,
  predicate text NOT NULL REFERENCES memory.predicate(predicate) ON DELETE RESTRICT,
  object_entity_id uuid,
  object_literal jsonb,
  qualifiers jsonb NOT NULL DEFAULT '{}'::jsonb,
  canonical_text text NOT NULL,
  canonical_key text NOT NULL,
  status memory.claim_status NOT NULL DEFAULT 'candidate',
  confidence numeric(4,3) NOT NULL DEFAULT 0.500,
  importance numeric(4,3) NOT NULL DEFAULT 0.500,
  salience numeric(4,3) NOT NULL DEFAULT 0.500,
  sensitivity memory.sensitivity_level NOT NULL DEFAULT 'medium',
  valid_from timestamptz,
  valid_to timestamptz,
  last_confirmed_at timestamptz,
  retrieval_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, canonical_key),
  UNIQUE (owner_user_id, claim_id),
  FOREIGN KEY (owner_user_id, subject_entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, object_entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id)
    ON DELETE RESTRICT,
  CHECK ((object_entity_id IS NULL) <> (object_literal IS NULL)),
  CHECK (btrim(canonical_text) <> ''),
  CHECK (btrim(canonical_key) <> ''),
  CHECK (confidence BETWEEN 0 AND 1),
  CHECK (importance BETWEEN 0 AND 1),
  CHECK (salience BETWEEN 0 AND 1),
  CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);

ALTER TABLE memory.claim
  ADD COLUMN IF NOT EXISTS qualifiers jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS claim_owner_active_idx
  ON memory.claim(owner_user_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS claim_owner_subject_predicate_idx
  ON memory.claim(owner_user_id, subject_entity_id, predicate);

CREATE INDEX IF NOT EXISTS claim_owner_retrieval_idx
  ON memory.claim(owner_user_id, salience DESC, importance DESC, confidence DESC)
  WHERE status IN ('supported', 'uncertain', 'disputed');

CREATE TABLE IF NOT EXISTS memory.claim_evidence (
  owner_user_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  stance memory.evidence_stance NOT NULL,
  relevance numeric(4,3) NOT NULL DEFAULT 1.000,
  rationale text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, claim_id, evidence_id, stance),
  FOREIGN KEY (owner_user_id, claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id)
    ON DELETE CASCADE,
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  CHECK (relevance BETWEEN 0 AND 1)
);

CREATE INDEX IF NOT EXISTS claim_evidence_evidence_idx
  ON memory.claim_evidence(owner_user_id, evidence_id);

CREATE TABLE IF NOT EXISTS memory.claim_relation (
  owner_user_id uuid NOT NULL,
  from_claim_id uuid NOT NULL,
  to_claim_id uuid NOT NULL,
  relation_type memory.claim_relation_type NOT NULL,
  rationale text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, from_claim_id, to_claim_id, relation_type),
  FOREIGN KEY (owner_user_id, from_claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id)
    ON DELETE CASCADE,
  FOREIGN KEY (owner_user_id, to_claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id)
    ON DELETE CASCADE,
  CHECK (from_claim_id <> to_claim_id)
);

CREATE TABLE IF NOT EXISTS memory.claim_assessment (
  assessment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  status memory.claim_status NOT NULL,
  support_score numeric(4,3),
  opposition_score numeric(4,3),
  confidence numeric(4,3) NOT NULL,
  method text NOT NULL,
  method_version text NOT NULL,
  rationale text,
  inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
  supersedes_assessment_id uuid,
  assessed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, assessment_id),
  FOREIGN KEY (owner_user_id, claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id)
    ON DELETE CASCADE,
  FOREIGN KEY (owner_user_id, supersedes_assessment_id)
    REFERENCES memory.claim_assessment(owner_user_id, assessment_id)
    ON DELETE RESTRICT,
  CHECK (support_score IS NULL OR support_score BETWEEN 0 AND 1),
  CHECK (opposition_score IS NULL OR opposition_score BETWEEN 0 AND 1),
  CHECK (confidence BETWEEN 0 AND 1),
  CHECK (btrim(method) <> ''),
  CHECK (btrim(method_version) <> '')
);

CREATE INDEX IF NOT EXISTS claim_assessment_history_idx
  ON memory.claim_assessment(owner_user_id, claim_id, assessed_at DESC);

CREATE TABLE IF NOT EXISTS memory.claim_revision (
  revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  revision_number integer NOT NULL,
  snapshot jsonb NOT NULL,
  reason text NOT NULL,
  actor_type text NOT NULL,
  actor_ref text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, claim_id, revision_number),
  FOREIGN KEY (owner_user_id, claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id)
    ON DELETE CASCADE,
  CHECK (revision_number > 0),
  CHECK (btrim(reason) <> ''),
  CHECK (actor_type IN ('user', 'system', 'job', 'admin'))
);

CREATE TABLE IF NOT EXISTS memory.candidate (
  candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  status memory.candidate_status NOT NULL DEFAULT 'proposed',
  proposal jsonb NOT NULL,
  comparison jsonb NOT NULL DEFAULT '{}'::jsonb,
  proposal_hash text NOT NULL,
  extractor text NOT NULL,
  extractor_version text NOT NULL,
  review_reason text,
  applied_claim_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, evidence_id, proposal_hash),
  UNIQUE (owner_user_id, candidate_id),
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE CASCADE,
  FOREIGN KEY (owner_user_id, applied_claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id)
    ON DELETE RESTRICT,
  CHECK (btrim(proposal_hash) <> ''),
  CHECK (btrim(extractor) <> ''),
  CHECK (btrim(extractor_version) <> '')
);

CREATE INDEX IF NOT EXISTS candidate_owner_status_idx
  ON memory.candidate(owner_user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS memory.user_preference (
  preference_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  preference_key text NOT NULL,
  value jsonb NOT NULL,
  status memory.record_status NOT NULL DEFAULT 'active',
  explicit boolean NOT NULL DEFAULT false,
  confidence numeric(4,3) NOT NULL DEFAULT 0.500,
  evidence_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, preference_key),
  UNIQUE (owner_user_id, preference_id),
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE SET NULL,
  CHECK (btrim(preference_key) <> ''),
  CHECK (confidence BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS memory.retrieval_trace (
  trace_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  request_id text,
  answer_id uuid,
  thread_id uuid,
  query_hash text NOT NULL,
  query_preview text,
  intent text NOT NULL,
  domain text NOT NULL,
  token_budget integer NOT NULL,
  selected_count integer NOT NULL DEFAULT 0,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, trace_id),
  CHECK (btrim(query_hash) <> ''),
  CHECK (token_budget >= 0),
  CHECK (selected_count >= 0)
);

CREATE INDEX IF NOT EXISTS retrieval_trace_owner_time_idx
  ON memory.retrieval_trace(owner_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory.retrieval_trace_item (
  owner_user_id uuid NOT NULL,
  trace_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  selected boolean NOT NULL,
  rank integer,
  semantic_score numeric,
  policy_score numeric,
  final_score numeric,
  reason_codes text[] NOT NULL DEFAULT '{}',
  prompt_tokens integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, trace_id, claim_id),
  FOREIGN KEY (owner_user_id, trace_id)
    REFERENCES memory.retrieval_trace(owner_user_id, trace_id)
    ON DELETE CASCADE,
  FOREIGN KEY (owner_user_id, claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id)
    ON DELETE RESTRICT,
  CHECK (rank IS NULL OR rank > 0),
  CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0)
);

CREATE TABLE IF NOT EXISTS memory.projection_outbox (
  outbox_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  aggregate_type text NOT NULL,
  aggregate_id uuid NOT NULL,
  operation text NOT NULL,
  payload jsonb NOT NULL,
  status memory.outbox_status NOT NULL DEFAULT 'pending',
  attempts integer NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT now(),
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, aggregate_type, aggregate_id, operation),
  CHECK (aggregate_type IN ('claim', 'evidence')),
  CHECK (operation IN ('upsert', 'delete')),
  CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS projection_outbox_pending_idx
  ON memory.projection_outbox(status, available_at, created_at)
  WHERE status IN ('pending', 'error');

DROP TRIGGER IF EXISTS entity_set_updated_at ON memory.entity;
CREATE TRIGGER entity_set_updated_at
BEFORE UPDATE ON memory.entity
FOR EACH ROW EXECUTE FUNCTION memory.set_updated_at();

DROP TRIGGER IF EXISTS claim_set_updated_at ON memory.claim;
CREATE TRIGGER claim_set_updated_at
BEFORE UPDATE ON memory.claim
FOR EACH ROW EXECUTE FUNCTION memory.set_updated_at();

DROP TRIGGER IF EXISTS candidate_set_updated_at ON memory.candidate;
CREATE TRIGGER candidate_set_updated_at
BEFORE UPDATE ON memory.candidate
FOR EACH ROW EXECUTE FUNCTION memory.set_updated_at();

DROP TRIGGER IF EXISTS preference_set_updated_at ON memory.user_preference;
CREATE TRIGGER preference_set_updated_at
BEFORE UPDATE ON memory.user_preference
FOR EACH ROW EXECUTE FUNCTION memory.set_updated_at();

DROP TRIGGER IF EXISTS outbox_set_updated_at ON memory.projection_outbox;
CREATE TRIGGER outbox_set_updated_at
BEFORE UPDATE ON memory.projection_outbox
FOR EACH ROW EXECUTE FUNCTION memory.set_updated_at();

INSERT INTO memory.predicate(predicate, object_kind, cardinality, description)
VALUES
  ('identity.preferred_name', 'literal', 'one', 'User preferred name'),
  ('relationship.kind', 'literal', 'many', 'Relationship between user and another entity'),
  ('personal_event.occurred', 'literal', 'many', 'Personal event with structured literal value'),
  ('name.canonical', 'literal', 'one', 'Canonical name after correction'),
  ('project.current', 'entity', 'many', 'Current project relationship'),
  ('life_context.active', 'literal', 'many', 'Active life context relevant to support')
ON CONFLICT (predicate) DO NOTHING;

REVOKE ALL ON SCHEMA memory FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA memory FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA memory FROM PUBLIC;

GRANT USAGE ON SCHEMA memory TO brains_app;
GRANT SELECT ON memory.predicate TO brains_app;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON memory.entity,
     memory.evidence,
     memory.entity_alias,
     memory.claim,
     memory.claim_evidence,
     memory.claim_relation,
     memory.claim_assessment,
     memory.claim_revision,
     memory.candidate,
     memory.user_preference,
     memory.retrieval_trace,
     memory.retrieval_trace_item,
     memory.projection_outbox
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;
GRANT EXECUTE ON FUNCTION memory.set_updated_at() TO brains_app;

ALTER DEFAULT PRIVILEGES FOR ROLE sage IN SCHEMA memory
  REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE sage IN SCHEMA memory
  REVOKE ALL ON FUNCTIONS FROM PUBLIC;

DO $rls$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'entity',
    'evidence',
    'entity_alias',
    'claim',
    'claim_evidence',
    'claim_relation',
    'claim_assessment',
    'claim_revision',
    'candidate',
    'user_preference',
    'retrieval_trace',
    'retrieval_trace_item',
    'projection_outbox'
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

COMMIT;
