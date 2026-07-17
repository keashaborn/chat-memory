BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 preference/project staging migration must run as sage, current_user=%',
      current_user;
  END IF;

  IF to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.evidence_lifecycle_event') IS NULL
     OR NOT EXISTS (
       SELECT 1 FROM pg_roles WHERE rolname = 'memory_evidence_maintainer'
     ) THEN
    RAISE EXCEPTION
      'memory V1 foundation and evidence lifecycle migrations are required';
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memory_review_maintainer') THEN
    CREATE ROLE memory_review_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$$;

ALTER ROLE memory_review_maintainer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

CREATE TABLE IF NOT EXISTS memory.preference_candidate (
  candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  preference_class text NOT NULL,
  preference_domain text NOT NULL,
  preference_key text NOT NULL,
  value jsonb NOT NULL,
  polarity text NOT NULL,
  scope jsonb NOT NULL DEFAULT '{}'::jsonb,
  explicit boolean NOT NULL DEFAULT false,
  stability text NOT NULL,
  surface_policy text NOT NULL,
  extraction_confidence numeric(4,3) NOT NULL,
  sensitivity memory.sensitivity_level NOT NULL DEFAULT 'medium',
  candidate_hash text NOT NULL,
  extractor text NOT NULL,
  extractor_version text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, candidate_id),
  UNIQUE (owner_user_id, candidate_hash),
  UNIQUE (owner_user_id, candidate_id, candidate_hash),
  CHECK (preference_class IN ('response', 'life')),
  CHECK (btrim(preference_domain) <> ''),
  CHECK (btrim(preference_key) <> ''),
  CHECK (jsonb_typeof(value) IS NOT NULL),
  CHECK (pg_column_size(value) <= 16384),
  CHECK (polarity IN ('prefer', 'avoid', 'require')),
  CHECK (jsonb_typeof(scope) = 'object'),
  CHECK (pg_column_size(scope) <= 16384),
  CHECK (stability IN ('tentative', 'contextual', 'stable')),
  CHECK (
    surface_policy IN (
      'silent_style_influence',
      'mention_when_relevant',
      'explicit_recall_only',
      'never_surface_as_content'
    )
  ),
  CHECK (extraction_confidence BETWEEN 0 AND 1),
  CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),
  CHECK (btrim(extractor) <> ''),
  CHECK (btrim(extractor_version) <> ''),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS preference_candidate_owner_key_time_idx
  ON memory.preference_candidate(
    owner_user_id, preference_class, preference_domain, preference_key,
    created_at DESC
  );

CREATE TABLE IF NOT EXISTS memory.preference_candidate_evidence (
  owner_user_id uuid NOT NULL,
  candidate_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  stance memory.evidence_stance NOT NULL DEFAULT 'supports',
  relevance numeric(4,3) NOT NULL DEFAULT 1.000,
  rationale text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, candidate_id, evidence_id),
  FOREIGN KEY (owner_user_id, candidate_id)
    REFERENCES memory.preference_candidate(owner_user_id, candidate_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  CHECK (relevance BETWEEN 0 AND 1),
  CHECK (rationale IS NULL OR btrim(rationale) <> '')
);

CREATE INDEX IF NOT EXISTS preference_candidate_evidence_owner_evidence_idx
  ON memory.preference_candidate_evidence(owner_user_id, evidence_id);

CREATE TABLE IF NOT EXISTS memory.preference_candidate_review (
  review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  candidate_id uuid NOT NULL,
  review_number integer NOT NULL,
  decision text NOT NULL,
  expected_candidate_hash text NOT NULL,
  reviewer_type text NOT NULL,
  reviewer_ref text,
  rationale text NOT NULL,
  reason_codes text[] NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  reviewed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, review_id),
  UNIQUE (owner_user_id, candidate_id, review_number),
  FOREIGN KEY (owner_user_id, candidate_id, expected_candidate_hash)
    REFERENCES memory.preference_candidate(
      owner_user_id, candidate_id, candidate_hash
    )
    ON DELETE RESTRICT,
  CHECK (review_number > 0),
  CHECK (decision IN ('accept', 'rewrite', 'reject', 'defer', 'split')),
  CHECK (expected_candidate_hash ~ '^[0-9a-f]{64}$'),
  CHECK (reviewer_type IN ('user', 'admin', 'job')),
  CHECK (reviewer_ref IS NULL OR length(reviewer_ref) <= 500),
  CHECK (btrim(rationale) <> ''),
  CHECK (cardinality(reason_codes) > 0),
  CHECK (array_position(reason_codes, NULL) IS NULL),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS preference_candidate_review_owner_candidate_idx
  ON memory.preference_candidate_review(
    owner_user_id, candidate_id, review_number DESC
  );

CREATE TABLE IF NOT EXISTS memory.preference_candidate_review_replacement (
  owner_user_id uuid NOT NULL,
  review_id uuid NOT NULL,
  replacement_candidate_id uuid NOT NULL,
  ordinal integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, review_id, replacement_candidate_id),
  UNIQUE (owner_user_id, review_id, ordinal),
  FOREIGN KEY (owner_user_id, review_id)
    REFERENCES memory.preference_candidate_review(owner_user_id, review_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, replacement_candidate_id)
    REFERENCES memory.preference_candidate(owner_user_id, candidate_id)
    ON DELETE RESTRICT,
  CHECK (ordinal > 0)
);

CREATE TABLE IF NOT EXISTS memory.project_space (
  project_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  project_key text NOT NULL,
  display_name text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, project_id),
  UNIQUE (owner_user_id, project_key),
  CHECK (btrim(project_key) <> ''),
  CHECK (btrim(display_name) <> ''),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE TABLE IF NOT EXISTS memory.project_knowledge_candidate (
  candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  knowledge_kind text NOT NULL,
  knowledge_key text NOT NULL,
  canonical_text text NOT NULL,
  document_state text NOT NULL,
  authority_level text NOT NULL,
  authority_source jsonb NOT NULL DEFAULT '{}'::jsonb,
  effective_at timestamptz,
  expires_at timestamptz,
  extraction_confidence numeric(4,3) NOT NULL,
  sensitivity memory.sensitivity_level NOT NULL DEFAULT 'medium',
  candidate_hash text NOT NULL,
  extractor text NOT NULL,
  extractor_version text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, project_id, candidate_id),
  UNIQUE (owner_user_id, project_id, candidate_hash),
  UNIQUE (owner_user_id, project_id, candidate_id, candidate_hash),
  FOREIGN KEY (owner_user_id, project_id)
    REFERENCES memory.project_space(owner_user_id, project_id)
    ON DELETE RESTRICT,
  CHECK (
    knowledge_kind IN (
      'architecture', 'constraint', 'decision', 'requirement', 'roadmap', 'status'
    )
  ),
  CHECK (btrim(knowledge_key) <> ''),
  CHECK (btrim(canonical_text) <> ''),
  CHECK (length(canonical_text) <= 16000),
  CHECK (
    document_state IN (
      'unverified', 'working', 'proposed', 'ratified', 'historical', 'superseded'
    )
  ),
  CHECK (
    authority_level IN (
      'unverified',
      'user_reported',
      'user_ratified',
      'approved_spec',
      'system_observed',
      'external_reference'
    )
  ),
  CHECK (jsonb_typeof(authority_source) = 'object'),
  CHECK (pg_column_size(authority_source) <= 16384),
  CHECK (expires_at IS NULL OR effective_at IS NULL OR expires_at > effective_at),
  CHECK (knowledge_kind <> 'status' OR effective_at IS NOT NULL),
  CHECK (extraction_confidence BETWEEN 0 AND 1),
  CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),
  CHECK (btrim(extractor) <> ''),
  CHECK (btrim(extractor_version) <> ''),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS project_knowledge_candidate_owner_key_time_idx
  ON memory.project_knowledge_candidate(
    owner_user_id, project_id, knowledge_kind, knowledge_key, created_at DESC
  );

CREATE TABLE IF NOT EXISTS memory.project_knowledge_candidate_evidence (
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  candidate_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  stance memory.evidence_stance NOT NULL DEFAULT 'supports',
  relevance numeric(4,3) NOT NULL DEFAULT 1.000,
  rationale text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, project_id, candidate_id, evidence_id),
  FOREIGN KEY (owner_user_id, project_id, candidate_id)
    REFERENCES memory.project_knowledge_candidate(
      owner_user_id, project_id, candidate_id
    )
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  CHECK (relevance BETWEEN 0 AND 1),
  CHECK (rationale IS NULL OR btrim(rationale) <> '')
);

CREATE INDEX IF NOT EXISTS project_candidate_evidence_owner_evidence_idx
  ON memory.project_knowledge_candidate_evidence(owner_user_id, evidence_id);

CREATE TABLE IF NOT EXISTS memory.project_knowledge_candidate_review (
  review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  candidate_id uuid NOT NULL,
  review_number integer NOT NULL,
  decision text NOT NULL,
  expected_candidate_hash text NOT NULL,
  reviewer_type text NOT NULL,
  reviewer_ref text,
  rationale text NOT NULL,
  reason_codes text[] NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  reviewed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, project_id, review_id),
  UNIQUE (owner_user_id, project_id, candidate_id, review_number),
  FOREIGN KEY (owner_user_id, project_id, candidate_id, expected_candidate_hash)
    REFERENCES memory.project_knowledge_candidate(
      owner_user_id, project_id, candidate_id, candidate_hash
    )
    ON DELETE RESTRICT,
  CHECK (review_number > 0),
  CHECK (decision IN ('accept', 'rewrite', 'reject', 'defer', 'split')),
  CHECK (expected_candidate_hash ~ '^[0-9a-f]{64}$'),
  CHECK (reviewer_type IN ('user', 'admin', 'job')),
  CHECK (reviewer_ref IS NULL OR length(reviewer_ref) <= 500),
  CHECK (btrim(rationale) <> ''),
  CHECK (cardinality(reason_codes) > 0),
  CHECK (array_position(reason_codes, NULL) IS NULL),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS project_candidate_review_owner_candidate_idx
  ON memory.project_knowledge_candidate_review(
    owner_user_id, project_id, candidate_id, review_number DESC
  );

CREATE TABLE IF NOT EXISTS memory.project_knowledge_candidate_review_replacement (
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  review_id uuid NOT NULL,
  replacement_candidate_id uuid NOT NULL,
  ordinal integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, project_id, review_id, replacement_candidate_id),
  UNIQUE (owner_user_id, project_id, review_id, ordinal),
  FOREIGN KEY (owner_user_id, project_id, review_id)
    REFERENCES memory.project_knowledge_candidate_review(
      owner_user_id, project_id, review_id
    )
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id, replacement_candidate_id)
    REFERENCES memory.project_knowledge_candidate(
      owner_user_id, project_id, candidate_id
    )
    ON DELETE RESTRICT,
  CHECK (ordinal > 0)
);

CREATE TABLE IF NOT EXISTS memory.project_knowledge_head (
  knowledge_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  knowledge_key text NOT NULL,
  knowledge_kind text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, project_id, knowledge_id),
  UNIQUE (owner_user_id, project_id, knowledge_key),
  FOREIGN KEY (owner_user_id, project_id)
    REFERENCES memory.project_space(owner_user_id, project_id)
    ON DELETE RESTRICT,
  CHECK (btrim(knowledge_key) <> ''),
  CHECK (
    knowledge_kind IN (
      'architecture', 'constraint', 'decision', 'requirement', 'roadmap', 'status'
    )
  )
);

CREATE TABLE IF NOT EXISTS memory.project_knowledge_revision (
  revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  knowledge_id uuid NOT NULL,
  revision_number integer NOT NULL,
  canonical_text text NOT NULL,
  content_sha256 text NOT NULL,
  document_state text NOT NULL,
  authority_level text NOT NULL,
  authority_source jsonb NOT NULL DEFAULT '{}'::jsonb,
  effective_from timestamptz,
  effective_to timestamptz,
  accepted_review_id uuid NOT NULL,
  supersedes_revision_id uuid,
  sensitivity memory.sensitivity_level NOT NULL DEFAULT 'medium',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, project_id, revision_id),
  UNIQUE (owner_user_id, project_id, knowledge_id, revision_number),
  UNIQUE (owner_user_id, project_id, knowledge_id, content_sha256),
  FOREIGN KEY (owner_user_id, project_id, knowledge_id)
    REFERENCES memory.project_knowledge_head(
      owner_user_id, project_id, knowledge_id
    )
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id, accepted_review_id)
    REFERENCES memory.project_knowledge_candidate_review(
      owner_user_id, project_id, review_id
    )
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id, supersedes_revision_id)
    REFERENCES memory.project_knowledge_revision(
      owner_user_id, project_id, revision_id
    )
    ON DELETE RESTRICT,
  CHECK (revision_number > 0),
  CHECK (btrim(canonical_text) <> ''),
  CHECK (length(canonical_text) <= 16000),
  CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (
    document_state IN (
      'working', 'proposed', 'ratified', 'historical', 'superseded'
    )
  ),
  CHECK (
    authority_level IN (
      'user_reported',
      'user_ratified',
      'approved_spec',
      'system_observed',
      'external_reference'
    )
  ),
  CHECK (jsonb_typeof(authority_source) = 'object'),
  CHECK (pg_column_size(authority_source) <= 16384),
  CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from),
  CHECK (supersedes_revision_id IS NULL OR supersedes_revision_id <> revision_id),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS project_revision_owner_head_number_idx
  ON memory.project_knowledge_revision(
    owner_user_id, project_id, knowledge_id, revision_number DESC
  );

CREATE UNIQUE INDEX IF NOT EXISTS project_revision_owner_accepted_review_uq
  ON memory.project_knowledge_revision(
    owner_user_id, project_id, accepted_review_id
  );

CREATE TABLE IF NOT EXISTS memory.project_knowledge_revision_evidence (
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  stance memory.evidence_stance NOT NULL DEFAULT 'supports',
  relevance numeric(4,3) NOT NULL DEFAULT 1.000,
  rationale text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, project_id, revision_id, evidence_id),
  FOREIGN KEY (owner_user_id, project_id, revision_id)
    REFERENCES memory.project_knowledge_revision(
      owner_user_id, project_id, revision_id
    )
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  CHECK (relevance BETWEEN 0 AND 1),
  CHECK (rationale IS NULL OR btrim(rationale) <> '')
);

CREATE INDEX IF NOT EXISTS project_revision_evidence_owner_evidence_idx
  ON memory.project_knowledge_revision_evidence(owner_user_id, evidence_id);

CREATE TABLE IF NOT EXISTS memory.project_knowledge_relation (
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  from_knowledge_id uuid NOT NULL,
  to_knowledge_id uuid NOT NULL,
  relation_type text NOT NULL,
  rationale text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (
    owner_user_id, project_id, from_knowledge_id, to_knowledge_id, relation_type
  ),
  FOREIGN KEY (owner_user_id, project_id, from_knowledge_id)
    REFERENCES memory.project_knowledge_head(
      owner_user_id, project_id, knowledge_id
    )
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id, to_knowledge_id)
    REFERENCES memory.project_knowledge_head(
      owner_user_id, project_id, knowledge_id
    )
    ON DELETE RESTRICT,
  CHECK (from_knowledge_id <> to_knowledge_id),
  CHECK (
    relation_type IN (
      'supersedes', 'conflicts', 'qualifies', 'depends_on', 'derived_from'
    )
  ),
  CHECK (rationale IS NULL OR btrim(rationale) <> '')
);

ALTER TABLE memory.evidence_lifecycle_event
  ADD COLUMN IF NOT EXISTS preference_candidate_link_count integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS project_candidate_link_count integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS project_revision_link_count integer NOT NULL DEFAULT 0;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.evidence_lifecycle_event'::regclass
      AND conname = 'evidence_lifecycle_specialized_counts_ck'
  ) THEN
    ALTER TABLE memory.evidence_lifecycle_event
      ADD CONSTRAINT evidence_lifecycle_specialized_counts_ck
      CHECK (
        preference_candidate_link_count >= 0
        AND project_candidate_link_count >= 0
        AND project_revision_link_count >= 0
      );
  END IF;
END
$$;

DO $$
BEGIN
  IF to_regprocedure('memory.guard_specialized_active_evidence()') IS NOT NULL THEN
    ALTER FUNCTION memory.guard_specialized_active_evidence() OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.populate_specialized_evidence_link_counts()'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.populate_specialized_evidence_link_counts() OWNER TO sage;
  END IF;
  IF to_regprocedure('memory.guard_preference_review_insert()') IS NOT NULL THEN
    ALTER FUNCTION memory.guard_preference_review_insert() OWNER TO sage;
  END IF;
  IF to_regprocedure('memory.guard_project_review_insert()') IS NOT NULL THEN
    ALTER FUNCTION memory.guard_project_review_insert() OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.guard_preference_review_replacement_insert()'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.guard_preference_review_replacement_insert()
      OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.guard_project_review_replacement_insert()'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.guard_project_review_replacement_insert()
      OWNER TO sage;
  END IF;
  IF to_regprocedure('memory.guard_project_revision_insert()') IS NOT NULL THEN
    ALTER FUNCTION memory.guard_project_revision_insert() OWNER TO sage;
  END IF;
END
$$;

CREATE OR REPLACE FUNCTION memory.guard_specialized_memory_append_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
    USING ERRCODE = '42501';
END
$$;

CREATE OR REPLACE FUNCTION memory.guard_specialized_active_evidence()
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
    RAISE EXCEPTION 'evidence link owner does not match the current actor'
      USING ERRCODE = '42501';
  END IF;

  SELECT evidence.status
  INTO evidence_status
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id = NEW.owner_user_id
    AND evidence.evidence_id = NEW.evidence_id
  FOR KEY SHARE;

  IF NOT FOUND OR evidence_status <> 'active' THEN
    RAISE EXCEPTION 'specialized memory records require active evidence'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION memory.populate_specialized_evidence_link_counts()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
  IF memory.current_actor_user_id() IS NULL
     OR NEW.owner_user_id <> memory.current_actor_user_id() THEN
    RAISE EXCEPTION 'lifecycle event owner does not match the current actor'
      USING ERRCODE = '42501';
  END IF;

  SELECT count(*)::integer
  INTO NEW.preference_candidate_link_count
  FROM memory.preference_candidate_evidence AS link
  WHERE link.owner_user_id = NEW.owner_user_id
    AND link.evidence_id = NEW.evidence_id;

  SELECT count(*)::integer
  INTO NEW.project_candidate_link_count
  FROM memory.project_knowledge_candidate_evidence AS link
  WHERE link.owner_user_id = NEW.owner_user_id
    AND link.evidence_id = NEW.evidence_id;

  SELECT count(*)::integer
  INTO NEW.project_revision_link_count
  FROM memory.project_knowledge_revision_evidence AS link
  WHERE link.owner_user_id = NEW.owner_user_id
    AND link.evidence_id = NEW.evidence_id;

  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION memory.guard_preference_review_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
  IF memory.current_actor_user_id() IS NULL
     OR NEW.owner_user_id <> memory.current_actor_user_id() THEN
    RAISE EXCEPTION 'preference review owner does not match the current actor'
      USING ERRCODE = '42501';
  END IF;

  PERFORM 1
  FROM memory.preference_candidate_evidence AS link
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = link.owner_user_id
   AND evidence.evidence_id = link.evidence_id
  WHERE link.owner_user_id = NEW.owner_user_id
    AND link.candidate_id = NEW.candidate_id
    AND evidence.status = 'active'
  FOR KEY SHARE OF evidence;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'preference review requires at least one active evidence link'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION memory.guard_project_review_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
  IF memory.current_actor_user_id() IS NULL
     OR NEW.owner_user_id <> memory.current_actor_user_id() THEN
    RAISE EXCEPTION 'project review owner does not match the current actor'
      USING ERRCODE = '42501';
  END IF;

  PERFORM 1
  FROM memory.project_knowledge_candidate_evidence AS link
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = link.owner_user_id
   AND evidence.evidence_id = link.evidence_id
  WHERE link.owner_user_id = NEW.owner_user_id
    AND link.project_id = NEW.project_id
    AND link.candidate_id = NEW.candidate_id
    AND evidence.status = 'active'
  FOR KEY SHARE OF evidence;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'project review requires at least one active evidence link'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION memory.guard_preference_review_replacement_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  review_decision text;
  source_candidate_id uuid;
  replacement_count integer;
BEGIN
  IF memory.current_actor_user_id() IS NULL
     OR NEW.owner_user_id <> memory.current_actor_user_id() THEN
    RAISE EXCEPTION 'preference replacement owner does not match the current actor'
      USING ERRCODE = '42501';
  END IF;

  SELECT review.decision, review.candidate_id
  INTO review_decision, source_candidate_id
  FROM memory.preference_candidate_review AS review
  WHERE review.owner_user_id = NEW.owner_user_id
    AND review.review_id = NEW.review_id;

  IF NOT FOUND OR review_decision NOT IN ('rewrite', 'split') THEN
    RAISE EXCEPTION 'replacement links require a rewrite or split review'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.replacement_candidate_id = source_candidate_id THEN
    RAISE EXCEPTION 'a review replacement cannot reference its source candidate'
      USING ERRCODE = '23514';
  END IF;

  SELECT count(*)::integer
  INTO replacement_count
  FROM memory.preference_candidate_review_replacement AS replacement
  WHERE replacement.owner_user_id = NEW.owner_user_id
    AND replacement.review_id = NEW.review_id;

  IF review_decision = 'rewrite'
     AND (NEW.ordinal <> 1 OR replacement_count <> 0) THEN
    RAISE EXCEPTION 'a rewrite review requires exactly one ordinal-1 replacement'
      USING ERRCODE = '23514';
  END IF;
  IF review_decision = 'split'
     AND NEW.ordinal <> replacement_count + 1 THEN
    RAISE EXCEPTION 'split replacements must use gapless ordinals'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION memory.guard_project_review_replacement_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  review_decision text;
  source_candidate_id uuid;
  replacement_count integer;
BEGIN
  IF memory.current_actor_user_id() IS NULL
     OR NEW.owner_user_id <> memory.current_actor_user_id() THEN
    RAISE EXCEPTION 'project replacement owner does not match the current actor'
      USING ERRCODE = '42501';
  END IF;

  SELECT review.decision, review.candidate_id
  INTO review_decision, source_candidate_id
  FROM memory.project_knowledge_candidate_review AS review
  WHERE review.owner_user_id = NEW.owner_user_id
    AND review.project_id = NEW.project_id
    AND review.review_id = NEW.review_id;

  IF NOT FOUND OR review_decision NOT IN ('rewrite', 'split') THEN
    RAISE EXCEPTION 'replacement links require a rewrite or split review'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.replacement_candidate_id = source_candidate_id THEN
    RAISE EXCEPTION 'a review replacement cannot reference its source candidate'
      USING ERRCODE = '23514';
  END IF;

  SELECT count(*)::integer
  INTO replacement_count
  FROM memory.project_knowledge_candidate_review_replacement AS replacement
  WHERE replacement.owner_user_id = NEW.owner_user_id
    AND replacement.project_id = NEW.project_id
    AND replacement.review_id = NEW.review_id;

  IF review_decision = 'rewrite'
     AND (NEW.ordinal <> 1 OR replacement_count <> 0) THEN
    RAISE EXCEPTION 'a rewrite review requires exactly one ordinal-1 replacement'
      USING ERRCODE = '23514';
  END IF;
  IF review_decision = 'split'
     AND NEW.ordinal <> replacement_count + 1 THEN
    RAISE EXCEPTION 'split replacements must use gapless ordinals'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION memory.guard_project_revision_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  candidate_row memory.project_knowledge_candidate%ROWTYPE;
  head_row memory.project_knowledge_head%ROWTYPE;
  prior_revision memory.project_knowledge_revision%ROWTYPE;
BEGIN
  IF memory.current_actor_user_id() IS NULL
     OR NEW.owner_user_id <> memory.current_actor_user_id() THEN
    RAISE EXCEPTION 'project revision owner does not match the current actor'
      USING ERRCODE = '42501';
  END IF;

  SELECT candidate.*
  INTO candidate_row
  FROM memory.project_knowledge_candidate_review AS review
  JOIN memory.project_knowledge_candidate AS candidate
    ON candidate.owner_user_id = review.owner_user_id
   AND candidate.project_id = review.project_id
   AND candidate.candidate_id = review.candidate_id
  WHERE review.owner_user_id = NEW.owner_user_id
    AND review.project_id = NEW.project_id
    AND review.review_id = NEW.accepted_review_id
    AND review.decision = 'accept';

  IF NOT FOUND THEN
    RAISE EXCEPTION 'project revision requires an accepted candidate review'
      USING ERRCODE = '23514';
  END IF;

  SELECT head.*
  INTO head_row
  FROM memory.project_knowledge_head AS head
  WHERE head.owner_user_id = NEW.owner_user_id
    AND head.project_id = NEW.project_id
    AND head.knowledge_id = NEW.knowledge_id;

  IF NOT FOUND
     OR head_row.knowledge_key <> candidate_row.knowledge_key
     OR head_row.knowledge_kind <> candidate_row.knowledge_kind
     OR NEW.canonical_text <> candidate_row.canonical_text
     OR NEW.content_sha256 <> candidate_row.candidate_hash
     OR NEW.document_state <> candidate_row.document_state
     OR NEW.authority_level <> candidate_row.authority_level
     OR NEW.authority_source <> candidate_row.authority_source
     OR NEW.effective_from IS DISTINCT FROM candidate_row.effective_at
     OR NEW.effective_to IS DISTINCT FROM candidate_row.expires_at
     OR NEW.sensitivity <> candidate_row.sensitivity
     OR NEW.metadata <> candidate_row.metadata THEN
    RAISE EXCEPTION 'project revision does not exactly match its accepted candidate'
      USING ERRCODE = '23514';
  END IF;

  IF NEW.revision_number = 1 THEN
    IF NEW.supersedes_revision_id IS NOT NULL OR EXISTS (
      SELECT 1
      FROM memory.project_knowledge_revision AS revision
      WHERE revision.owner_user_id = NEW.owner_user_id
        AND revision.project_id = NEW.project_id
        AND revision.knowledge_id = NEW.knowledge_id
    ) THEN
      RAISE EXCEPTION 'revision 1 must be the first revision and supersede nothing'
        USING ERRCODE = '23514';
    END IF;
  ELSE
    IF NEW.supersedes_revision_id IS NULL THEN
      RAISE EXCEPTION 'revision numbers above 1 must supersede the prior revision'
        USING ERRCODE = '23514';
    END IF;

    SELECT revision.*
    INTO prior_revision
    FROM memory.project_knowledge_revision AS revision
    WHERE revision.owner_user_id = NEW.owner_user_id
      AND revision.project_id = NEW.project_id
      AND revision.revision_id = NEW.supersedes_revision_id;

    IF NOT FOUND
       OR prior_revision.knowledge_id <> NEW.knowledge_id
       OR prior_revision.revision_number <> NEW.revision_number - 1 THEN
      RAISE EXCEPTION 'project revision ancestry must be same-head and gapless'
        USING ERRCODE = '23514';
    END IF;
  END IF;

  RETURN NEW;
END
$$;

DO $triggers$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'preference_candidate',
    'preference_candidate_evidence',
    'preference_candidate_review',
    'preference_candidate_review_replacement',
    'project_space',
    'project_knowledge_candidate',
    'project_knowledge_candidate_evidence',
    'project_knowledge_candidate_review',
    'project_knowledge_candidate_review_replacement',
    'project_knowledge_head',
    'project_knowledge_revision',
    'project_knowledge_revision_evidence',
    'project_knowledge_relation'
  ]
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS specialized_append_only_guard ON memory.%I',
      table_name
    );
    EXECUTE format(
      'CREATE TRIGGER specialized_append_only_guard '
      'BEFORE UPDATE OR DELETE ON memory.%I '
      'FOR EACH ROW EXECUTE FUNCTION memory.guard_specialized_memory_append_only()',
      table_name
    );
  END LOOP;
END
$triggers$;

DROP TRIGGER IF EXISTS preference_candidate_active_evidence_guard
  ON memory.preference_candidate_evidence;
CREATE TRIGGER preference_candidate_active_evidence_guard
BEFORE INSERT ON memory.preference_candidate_evidence
FOR EACH ROW EXECUTE FUNCTION memory.guard_specialized_active_evidence();

DROP TRIGGER IF EXISTS project_candidate_active_evidence_guard
  ON memory.project_knowledge_candidate_evidence;
CREATE TRIGGER project_candidate_active_evidence_guard
BEFORE INSERT ON memory.project_knowledge_candidate_evidence
FOR EACH ROW EXECUTE FUNCTION memory.guard_specialized_active_evidence();

DROP TRIGGER IF EXISTS project_revision_active_evidence_guard
  ON memory.project_knowledge_revision_evidence;
CREATE TRIGGER project_revision_active_evidence_guard
BEFORE INSERT ON memory.project_knowledge_revision_evidence
FOR EACH ROW EXECUTE FUNCTION memory.guard_specialized_active_evidence();

DROP TRIGGER IF EXISTS preference_review_insert_guard
  ON memory.preference_candidate_review;
CREATE TRIGGER preference_review_insert_guard
BEFORE INSERT ON memory.preference_candidate_review
FOR EACH ROW EXECUTE FUNCTION memory.guard_preference_review_insert();

DROP TRIGGER IF EXISTS project_review_insert_guard
  ON memory.project_knowledge_candidate_review;
CREATE TRIGGER project_review_insert_guard
BEFORE INSERT ON memory.project_knowledge_candidate_review
FOR EACH ROW EXECUTE FUNCTION memory.guard_project_review_insert();

DROP TRIGGER IF EXISTS preference_review_replacement_insert_guard
  ON memory.preference_candidate_review_replacement;
CREATE TRIGGER preference_review_replacement_insert_guard
BEFORE INSERT ON memory.preference_candidate_review_replacement
FOR EACH ROW
EXECUTE FUNCTION memory.guard_preference_review_replacement_insert();

DROP TRIGGER IF EXISTS project_review_replacement_insert_guard
  ON memory.project_knowledge_candidate_review_replacement;
CREATE TRIGGER project_review_replacement_insert_guard
BEFORE INSERT ON memory.project_knowledge_candidate_review_replacement
FOR EACH ROW EXECUTE FUNCTION memory.guard_project_review_replacement_insert();

DROP TRIGGER IF EXISTS project_revision_insert_guard
  ON memory.project_knowledge_revision;
CREATE TRIGGER project_revision_insert_guard
BEFORE INSERT ON memory.project_knowledge_revision
FOR EACH ROW EXECUTE FUNCTION memory.guard_project_revision_insert();

DROP TRIGGER IF EXISTS evidence_lifecycle_specialized_counts
  ON memory.evidence_lifecycle_event;
CREATE TRIGGER evidence_lifecycle_specialized_counts
BEFORE INSERT ON memory.evidence_lifecycle_event
FOR EACH ROW EXECUTE FUNCTION memory.populate_specialized_evidence_link_counts();

DO $rls$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'preference_candidate',
    'preference_candidate_evidence',
    'preference_candidate_review',
    'preference_candidate_review_replacement',
    'project_space',
    'project_knowledge_candidate',
    'project_knowledge_candidate_evidence',
    'project_knowledge_candidate_review',
    'project_knowledge_candidate_review_replacement',
    'project_knowledge_head',
    'project_knowledge_revision',
    'project_knowledge_revision_evidence',
    'project_knowledge_relation'
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

REVOKE ALL ON
  memory.preference_candidate,
  memory.preference_candidate_evidence,
  memory.preference_candidate_review,
  memory.preference_candidate_review_replacement,
  memory.project_space,
  memory.project_knowledge_candidate,
  memory.project_knowledge_candidate_evidence,
  memory.project_knowledge_candidate_review,
  memory.project_knowledge_candidate_review_replacement,
  memory.project_knowledge_head,
  memory.project_knowledge_revision,
  memory.project_knowledge_revision_evidence,
  memory.project_knowledge_relation
FROM PUBLIC, brains_app;

GRANT SELECT, INSERT ON
  memory.preference_candidate,
  memory.preference_candidate_evidence,
  memory.project_knowledge_candidate,
  memory.project_knowledge_candidate_evidence
TO brains_app;

GRANT SELECT ON
  memory.preference_candidate_review,
  memory.preference_candidate_review_replacement,
  memory.project_space,
  memory.project_knowledge_candidate_review,
  memory.project_knowledge_candidate_review_replacement,
  memory.project_knowledge_head,
  memory.project_knowledge_revision,
  memory.project_knowledge_revision_evidence,
  memory.project_knowledge_relation
TO brains_app;

REVOKE INSERT, UPDATE, DELETE ON memory.user_preference FROM brains_app;
GRANT SELECT ON memory.user_preference TO brains_app;

GRANT SELECT ON
  memory.preference_candidate_evidence,
  memory.project_knowledge_candidate_evidence,
  memory.project_knowledge_revision_evidence
TO memory_evidence_maintainer;

GRANT USAGE ON SCHEMA memory TO memory_review_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_review_maintainer;
GRANT SELECT, UPDATE ON memory.evidence TO memory_review_maintainer;
GRANT SELECT ON
  memory.preference_candidate,
  memory.preference_candidate_evidence,
  memory.preference_candidate_review,
  memory.preference_candidate_review_replacement,
  memory.project_knowledge_candidate,
  memory.project_knowledge_candidate_evidence,
  memory.project_knowledge_candidate_review,
  memory.project_knowledge_candidate_review_replacement,
  memory.project_knowledge_head,
  memory.project_knowledge_revision
TO memory_review_maintainer;

GRANT CREATE ON SCHEMA memory TO memory_evidence_maintainer;
ALTER FUNCTION memory.guard_specialized_active_evidence()
  OWNER TO memory_evidence_maintainer;
ALTER FUNCTION memory.populate_specialized_evidence_link_counts()
  OWNER TO memory_evidence_maintainer;
REVOKE CREATE ON SCHEMA memory FROM memory_evidence_maintainer;

GRANT CREATE ON SCHEMA memory TO memory_review_maintainer;
ALTER FUNCTION memory.guard_preference_review_insert()
  OWNER TO memory_review_maintainer;
ALTER FUNCTION memory.guard_project_review_insert()
  OWNER TO memory_review_maintainer;
ALTER FUNCTION memory.guard_preference_review_replacement_insert()
  OWNER TO memory_review_maintainer;
ALTER FUNCTION memory.guard_project_review_replacement_insert()
  OWNER TO memory_review_maintainer;
ALTER FUNCTION memory.guard_project_revision_insert()
  OWNER TO memory_review_maintainer;
REVOKE CREATE ON SCHEMA memory FROM memory_review_maintainer;

REVOKE ALL ON FUNCTION memory.guard_specialized_memory_append_only() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.guard_specialized_active_evidence() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.populate_specialized_evidence_link_counts() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.guard_preference_review_insert() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.guard_project_review_insert() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.guard_preference_review_replacement_insert()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.guard_project_review_replacement_insert()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.guard_project_revision_insert() FROM PUBLIC;

COMMIT;
