BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 review/apply migration must run as sage, current_user=%',
      current_user;
  END IF;
  IF to_regclass('memory.preference_candidate') IS NULL
     OR to_regclass('memory.project_knowledge_candidate') IS NULL
     OR NOT EXISTS (
       SELECT 1 FROM pg_roles WHERE rolname = 'memory_review_maintainer'
     ) THEN
    RAISE EXCEPTION 'preference/project staging migration is required';
  END IF;
  IF EXISTS (SELECT 1 FROM memory.user_preference LIMIT 1)
     AND NOT EXISTS (
       SELECT 1
       FROM information_schema.columns
       WHERE table_schema = 'memory'
         AND table_name = 'user_preference'
         AND column_name = 'current_revision_id'
     ) THEN
    RAISE EXCEPTION
      'legacy user_preference rows require a reviewed backfill before this migration';
  END IF;
END
$$;

ALTER TABLE memory.preference_candidate_review
  ADD COLUMN IF NOT EXISTS request_id uuid,
  ADD COLUMN IF NOT EXISTS request_sha256 text;

ALTER TABLE memory.project_knowledge_candidate_review
  ADD COLUMN IF NOT EXISTS request_id uuid,
  ADD COLUMN IF NOT EXISTS request_sha256 text;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.preference_candidate_review
    WHERE request_id IS NULL OR request_sha256 IS NULL
  ) OR EXISTS (
    SELECT 1 FROM memory.project_knowledge_candidate_review
    WHERE request_id IS NULL OR request_sha256 IS NULL
  ) THEN
    RAISE EXCEPTION 'existing review rows require request-id backfill';
  END IF;
END
$$;

ALTER TABLE memory.preference_candidate_review
  ALTER COLUMN request_id SET NOT NULL,
  ALTER COLUMN request_sha256 SET NOT NULL;

ALTER TABLE memory.project_knowledge_candidate_review
  ALTER COLUMN request_id SET NOT NULL,
  ALTER COLUMN request_sha256 SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS preference_review_owner_request_uq
  ON memory.preference_candidate_review(owner_user_id, request_id);

CREATE UNIQUE INDEX IF NOT EXISTS project_review_owner_request_uq
  ON memory.project_knowledge_candidate_review(owner_user_id, request_id);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.preference_candidate_review'::regclass
      AND conname = 'preference_review_request_sha_ck'
  ) THEN
    ALTER TABLE memory.preference_candidate_review
      ADD CONSTRAINT preference_review_request_sha_ck
      CHECK (request_sha256 ~ '^[0-9a-f]{64}$');
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.project_knowledge_candidate_review'::regclass
      AND conname = 'project_review_request_sha_ck'
  ) THEN
    ALTER TABLE memory.project_knowledge_candidate_review
      ADD CONSTRAINT project_review_request_sha_ck
      CHECK (request_sha256 ~ '^[0-9a-f]{64}$');
  END IF;
END
$$;

ALTER TABLE memory.user_preference
  ADD COLUMN IF NOT EXISTS preference_class text,
  ADD COLUMN IF NOT EXISTS preference_domain text,
  ADD COLUMN IF NOT EXISTS polarity text,
  ADD COLUMN IF NOT EXISTS scope jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS stability text,
  ADD COLUMN IF NOT EXISTS surface_policy text,
  ADD COLUMN IF NOT EXISTS sensitivity memory.sensitivity_level,
  ADD COLUMN IF NOT EXISTS current_revision_id uuid,
  ADD COLUMN IF NOT EXISTS revision_number integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS content_sha256 text,
  ADD COLUMN IF NOT EXISTS accepted_review_id uuid;

ALTER TABLE memory.user_preference
  ALTER COLUMN preference_class SET NOT NULL,
  ALTER COLUMN preference_domain SET NOT NULL,
  ALTER COLUMN polarity SET NOT NULL,
  ALTER COLUMN stability SET NOT NULL,
  ALTER COLUMN surface_policy SET NOT NULL,
  ALTER COLUMN sensitivity SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.user_preference'::regclass
      AND conname = 'user_preference_reviewed_shape_ck'
  ) THEN
    ALTER TABLE memory.user_preference
      ADD CONSTRAINT user_preference_reviewed_shape_ck
      CHECK (
        preference_class IN ('response', 'life')
        AND btrim(preference_domain) <> ''
        AND polarity IN ('prefer', 'avoid', 'require')
        AND jsonb_typeof(scope) = 'object'
        AND pg_column_size(scope) <= 16384
        AND stability IN ('tentative', 'contextual', 'stable')
        AND surface_policy IN (
          'silent_style_influence',
          'mention_when_relevant',
          'explicit_recall_only',
          'never_surface_as_content'
        )
        AND (
          (
            current_revision_id IS NULL
            AND revision_number = 0
            AND content_sha256 IS NULL
            AND accepted_review_id IS NULL
          )
          OR (
            current_revision_id IS NOT NULL
            AND revision_number > 0
            AND content_sha256 ~ '^[0-9a-f]{64}$'
            AND accepted_review_id IS NOT NULL
          )
        )
      );
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS memory.preference_revision (
  revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  preference_id uuid NOT NULL,
  candidate_id uuid NOT NULL,
  accepted_review_id uuid NOT NULL,
  revision_number integer NOT NULL,
  preference_class text NOT NULL,
  preference_domain text NOT NULL,
  preference_key text NOT NULL,
  value jsonb NOT NULL,
  polarity text NOT NULL,
  scope jsonb NOT NULL DEFAULT '{}'::jsonb,
  explicit boolean NOT NULL,
  stability text NOT NULL,
  surface_policy text NOT NULL,
  extraction_confidence numeric(4,3) NOT NULL,
  sensitivity memory.sensitivity_level NOT NULL,
  content_sha256 text NOT NULL,
  prior_revision_id uuid,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, revision_id),
  UNIQUE (owner_user_id, preference_id, revision_number),
  UNIQUE (owner_user_id, accepted_review_id),
  FOREIGN KEY (owner_user_id, preference_id)
    REFERENCES memory.user_preference(owner_user_id, preference_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, candidate_id)
    REFERENCES memory.preference_candidate(owner_user_id, candidate_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, accepted_review_id)
    REFERENCES memory.preference_candidate_review(owner_user_id, review_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, prior_revision_id)
    REFERENCES memory.preference_revision(owner_user_id, revision_id)
    ON DELETE RESTRICT,
  CHECK (revision_number > 0),
  CHECK (preference_class IN ('response', 'life')),
  CHECK (btrim(preference_domain) <> ''),
  CHECK (btrim(preference_key) <> ''),
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
  CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (prior_revision_id IS NULL OR prior_revision_id <> revision_id),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS preference_revision_owner_head_number_idx
  ON memory.preference_revision(
    owner_user_id, preference_id, revision_number DESC
  );

CREATE TABLE IF NOT EXISTS memory.preference_revision_evidence (
  owner_user_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  stance memory.evidence_stance NOT NULL DEFAULT 'supports',
  relevance numeric(4,3) NOT NULL DEFAULT 1.000,
  rationale text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, revision_id, evidence_id),
  FOREIGN KEY (owner_user_id, revision_id)
    REFERENCES memory.preference_revision(owner_user_id, revision_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  CHECK (relevance BETWEEN 0 AND 1),
  CHECK (rationale IS NULL OR btrim(rationale) <> '')
);

CREATE INDEX IF NOT EXISTS preference_revision_evidence_owner_evidence_idx
  ON memory.preference_revision_evidence(owner_user_id, evidence_id);

CREATE TABLE IF NOT EXISTS memory.preference_apply_event (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  request_id uuid NOT NULL,
  request_sha256 text NOT NULL,
  candidate_id uuid NOT NULL,
  accepted_review_id uuid NOT NULL,
  preference_id uuid NOT NULL,
  prior_revision_id uuid,
  resulting_revision_id uuid NOT NULL,
  outcome text NOT NULL DEFAULT 'applied',
  actor_user_id uuid NOT NULL,
  invoked_by_role name NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, event_id),
  UNIQUE (owner_user_id, request_id),
  UNIQUE (owner_user_id, accepted_review_id),
  FOREIGN KEY (owner_user_id, candidate_id)
    REFERENCES memory.preference_candidate(owner_user_id, candidate_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, accepted_review_id)
    REFERENCES memory.preference_candidate_review(owner_user_id, review_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, preference_id)
    REFERENCES memory.user_preference(owner_user_id, preference_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, prior_revision_id)
    REFERENCES memory.preference_revision(owner_user_id, revision_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, resulting_revision_id)
    REFERENCES memory.preference_revision(owner_user_id, revision_id)
    ON DELETE RESTRICT,
  CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (outcome = 'applied'),
  CHECK (actor_user_id = owner_user_id),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS preference_apply_event_owner_time_idx
  ON memory.preference_apply_event(owner_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory.project_space_registration_event (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  request_id uuid NOT NULL,
  request_sha256 text NOT NULL,
  project_id uuid NOT NULL,
  outcome text NOT NULL,
  actor_user_id uuid NOT NULL,
  invoked_by_role name NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, event_id),
  UNIQUE (owner_user_id, request_id),
  FOREIGN KEY (owner_user_id, project_id)
    REFERENCES memory.project_space(owner_user_id, project_id)
    ON DELETE RESTRICT,
  CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (outcome IN ('created', 'existing')),
  CHECK (actor_user_id = owner_user_id),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS project_registration_event_owner_time_idx
  ON memory.project_space_registration_event(owner_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory.project_knowledge_apply_event (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  request_id uuid NOT NULL,
  request_sha256 text NOT NULL,
  candidate_id uuid NOT NULL,
  accepted_review_id uuid NOT NULL,
  knowledge_id uuid NOT NULL,
  prior_revision_id uuid,
  resulting_revision_id uuid NOT NULL,
  outcome text NOT NULL DEFAULT 'applied',
  actor_user_id uuid NOT NULL,
  invoked_by_role name NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, project_id, event_id),
  UNIQUE (owner_user_id, request_id),
  UNIQUE (owner_user_id, project_id, accepted_review_id),
  FOREIGN KEY (owner_user_id, project_id, candidate_id)
    REFERENCES memory.project_knowledge_candidate(
      owner_user_id, project_id, candidate_id
    )
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id, accepted_review_id)
    REFERENCES memory.project_knowledge_candidate_review(
      owner_user_id, project_id, review_id
    )
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id, knowledge_id)
    REFERENCES memory.project_knowledge_head(
      owner_user_id, project_id, knowledge_id
    )
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id, prior_revision_id)
    REFERENCES memory.project_knowledge_revision(
      owner_user_id, project_id, revision_id
    )
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id, resulting_revision_id)
    REFERENCES memory.project_knowledge_revision(
      owner_user_id, project_id, revision_id
    )
    ON DELETE RESTRICT,
  CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (outcome = 'applied'),
  CHECK (actor_user_id = owner_user_id),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS project_apply_event_owner_time_idx
  ON memory.project_knowledge_apply_event(
    owner_user_id, project_id, created_at DESC
  );

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.user_preference'::regclass
      AND conname = 'user_preference_current_revision_fk'
  ) THEN
    ALTER TABLE memory.user_preference
      ADD CONSTRAINT user_preference_current_revision_fk
      FOREIGN KEY (owner_user_id, current_revision_id)
      REFERENCES memory.preference_revision(owner_user_id, revision_id)
      ON DELETE RESTRICT;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.user_preference'::regclass
      AND conname = 'user_preference_accepted_review_fk'
  ) THEN
    ALTER TABLE memory.user_preference
      ADD CONSTRAINT user_preference_accepted_review_fk
      FOREIGN KEY (owner_user_id, accepted_review_id)
      REFERENCES memory.preference_candidate_review(owner_user_id, review_id)
      ON DELETE RESTRICT;
  END IF;
END
$$;

DO $$
BEGIN
  IF to_regprocedure('memory.review_request_sha(jsonb)') IS NOT NULL THEN
    ALTER FUNCTION memory.review_request_sha(jsonb) OWNER TO sage;
  END IF;
  IF to_regprocedure('memory.guard_user_preference_controlled_write()') IS NOT NULL THEN
    ALTER FUNCTION memory.guard_user_preference_controlled_write() OWNER TO sage;
  END IF;
  IF to_regprocedure('memory.guard_preference_revision_insert()') IS NOT NULL THEN
    ALTER FUNCTION memory.guard_preference_revision_insert() OWNER TO sage;
  END IF;
  IF to_regprocedure('memory.register_project_space(uuid,text,text,jsonb)') IS NOT NULL THEN
    ALTER FUNCTION memory.register_project_space(uuid,text,text,jsonb) OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.review_preference_candidate(uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.review_preference_candidate(
      uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb
    ) OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.review_project_candidate(uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.review_project_candidate(
      uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb
    ) OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.apply_preference_candidate(uuid,uuid,uuid,jsonb)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.apply_preference_candidate(uuid,uuid,uuid,jsonb)
      OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.apply_project_candidate(uuid,uuid,uuid,jsonb)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.apply_project_candidate(uuid,uuid,uuid,jsonb)
      OWNER TO sage;
  END IF;
END
$$;

CREATE OR REPLACE FUNCTION memory.review_request_sha(p_payload jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
  SELECT encode(public.digest(convert_to(p_payload::text, 'UTF8'), 'sha256'), 'hex')
$$;

CREATE OR REPLACE FUNCTION memory.guard_user_preference_controlled_write()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
  actor uuid;
  revision memory.preference_revision%ROWTYPE;
BEGIN
  IF current_user <> 'memory_review_maintainer' THEN
    RAISE EXCEPTION
      'active preferences may only change through the controlled apply function'
      USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'active preferences cannot be physically deleted'
      USING ERRCODE = '42501';
  END IF;

  actor := memory.current_actor_user_id();
  IF actor IS NULL OR NEW.owner_user_id <> actor THEN
    RAISE EXCEPTION 'preference owner does not match the current actor'
      USING ERRCODE = '42501';
  END IF;
  IF NEW.status <> 'active' THEN
    RAISE EXCEPTION 'the active preference snapshot must remain active'
      USING ERRCODE = '23514';
  END IF;

  IF TG_OP = 'INSERT' THEN
    IF NEW.current_revision_id IS NOT NULL
       OR NEW.revision_number <> 0
       OR NEW.content_sha256 IS NOT NULL
       OR NEW.accepted_review_id IS NOT NULL THEN
      RAISE EXCEPTION 'a new preference head must start as an unapplied placeholder'
        USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.preference_id <> OLD.preference_id
     OR NEW.owner_user_id <> OLD.owner_user_id
     OR NEW.preference_key <> OLD.preference_key
     OR NEW.revision_number <> OLD.revision_number + 1
     OR NEW.current_revision_id IS NULL
     OR NEW.current_revision_id IS NOT DISTINCT FROM OLD.current_revision_id THEN
    RAISE EXCEPTION 'preference updates must advance exactly one immutable revision'
      USING ERRCODE = '23514';
  END IF;

  SELECT stored.*
  INTO revision
  FROM memory.preference_revision AS stored
  WHERE stored.owner_user_id = NEW.owner_user_id
    AND stored.preference_id = NEW.preference_id
    AND stored.revision_id = NEW.current_revision_id;

  IF NOT FOUND
     OR revision.revision_number <> NEW.revision_number
     OR revision.preference_class <> NEW.preference_class
     OR revision.preference_domain <> NEW.preference_domain
     OR revision.preference_key <> NEW.preference_key
     OR revision.value <> NEW.value
     OR revision.polarity <> NEW.polarity
     OR revision.scope <> NEW.scope
     OR revision.explicit <> NEW.explicit
     OR revision.stability <> NEW.stability
     OR revision.surface_policy <> NEW.surface_policy
     OR revision.extraction_confidence <> NEW.confidence
     OR revision.sensitivity <> NEW.sensitivity
     OR revision.content_sha256 <> NEW.content_sha256
     OR revision.accepted_review_id <> NEW.accepted_review_id
     OR NEW.evidence_id IS NULL
     OR NOT EXISTS (
       SELECT 1
       FROM memory.preference_revision_evidence AS link
       WHERE link.owner_user_id = NEW.owner_user_id
         AND link.revision_id = NEW.current_revision_id
         AND link.evidence_id = NEW.evidence_id
     ) THEN
    RAISE EXCEPTION 'active preference does not exactly match its durable revision'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION memory.guard_preference_revision_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
  actor uuid;
  candidate memory.preference_candidate%ROWTYPE;
  review memory.preference_candidate_review%ROWTYPE;
  head memory.user_preference%ROWTYPE;
  prior memory.preference_revision%ROWTYPE;
BEGIN
  IF current_user <> 'memory_review_maintainer' THEN
    RAISE EXCEPTION
      'preference revisions may only be inserted by the controlled apply function'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL OR NEW.owner_user_id <> actor THEN
    RAISE EXCEPTION 'preference revision owner does not match the current actor'
      USING ERRCODE = '42501';
  END IF;

  SELECT stored.*
  INTO review
  FROM memory.preference_candidate_review AS stored
  WHERE stored.owner_user_id = actor
    AND stored.review_id = NEW.accepted_review_id
    AND stored.decision = 'accept'
    AND NOT EXISTS (
      SELECT 1
      FROM memory.preference_candidate_review AS newer
      WHERE newer.owner_user_id = stored.owner_user_id
        AND newer.candidate_id = stored.candidate_id
        AND newer.review_number > stored.review_number
    );
  IF NOT FOUND THEN
    RAISE EXCEPTION 'preference revision requires the latest accepted review'
      USING ERRCODE = '23514';
  END IF;

  SELECT stored.*
  INTO candidate
  FROM memory.preference_candidate AS stored
  WHERE stored.owner_user_id = actor
    AND stored.candidate_id = review.candidate_id;
  SELECT stored.*
  INTO head
  FROM memory.user_preference AS stored
  WHERE stored.owner_user_id = actor
    AND stored.preference_id = NEW.preference_id;

  IF NOT FOUND
     OR NEW.candidate_id <> candidate.candidate_id
     OR NEW.preference_class <> candidate.preference_class
     OR NEW.preference_domain <> candidate.preference_domain
     OR NEW.preference_key <> candidate.preference_key
     OR NEW.value <> candidate.value
     OR NEW.polarity <> candidate.polarity
     OR NEW.scope <> candidate.scope
     OR NEW.explicit <> candidate.explicit
     OR NEW.stability <> candidate.stability
     OR NEW.surface_policy <> candidate.surface_policy
     OR NEW.extraction_confidence <> candidate.extraction_confidence
     OR NEW.sensitivity <> candidate.sensitivity
     OR NEW.content_sha256 <> candidate.candidate_hash
     OR NEW.metadata <> candidate.metadata
     OR head.preference_key <> candidate.preference_key THEN
    RAISE EXCEPTION 'preference revision does not exactly match its accepted candidate'
      USING ERRCODE = '23514';
  END IF;

  IF NEW.revision_number = 1 THEN
    IF NEW.prior_revision_id IS NOT NULL OR EXISTS (
      SELECT 1
      FROM memory.preference_revision AS stored
      WHERE stored.owner_user_id = actor
        AND stored.preference_id = NEW.preference_id
    ) THEN
      RAISE EXCEPTION 'revision 1 must be first and have no prior revision'
        USING ERRCODE = '23514';
    END IF;
  ELSE
    SELECT stored.*
    INTO prior
    FROM memory.preference_revision AS stored
    WHERE stored.owner_user_id = actor
      AND stored.revision_id = NEW.prior_revision_id;
    IF NOT FOUND
       OR prior.preference_id <> NEW.preference_id
       OR prior.revision_number <> NEW.revision_number - 1 THEN
      RAISE EXCEPTION 'preference revision ancestry must be same-head and gapless'
        USING ERRCODE = '23514';
    END IF;
  END IF;

  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS user_preference_controlled_write_guard
  ON memory.user_preference;
CREATE TRIGGER user_preference_controlled_write_guard
BEFORE INSERT OR UPDATE OR DELETE ON memory.user_preference
FOR EACH ROW EXECUTE FUNCTION memory.guard_user_preference_controlled_write();

DROP TRIGGER IF EXISTS preference_revision_insert_guard
  ON memory.preference_revision;
CREATE TRIGGER preference_revision_insert_guard
BEFORE INSERT ON memory.preference_revision
FOR EACH ROW EXECUTE FUNCTION memory.guard_preference_revision_insert();

DROP TRIGGER IF EXISTS specialized_append_only_guard
  ON memory.preference_revision;
CREATE TRIGGER specialized_append_only_guard
BEFORE UPDATE OR DELETE ON memory.preference_revision
FOR EACH ROW EXECUTE FUNCTION memory.guard_specialized_memory_append_only();

DROP TRIGGER IF EXISTS specialized_append_only_guard
  ON memory.preference_revision_evidence;
CREATE TRIGGER specialized_append_only_guard
BEFORE UPDATE OR DELETE ON memory.preference_revision_evidence
FOR EACH ROW EXECUTE FUNCTION memory.guard_specialized_memory_append_only();

DROP TRIGGER IF EXISTS preference_revision_active_evidence_guard
  ON memory.preference_revision_evidence;
CREATE TRIGGER preference_revision_active_evidence_guard
BEFORE INSERT ON memory.preference_revision_evidence
FOR EACH ROW EXECUTE FUNCTION memory.guard_specialized_active_evidence();

DO $events$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'preference_apply_event',
    'project_space_registration_event',
    'project_knowledge_apply_event'
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
$events$;

CREATE OR REPLACE FUNCTION memory.register_project_space(
  p_request_id uuid,
  p_project_key text,
  p_display_name text,
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS SETOF memory.project_space
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $$
DECLARE
  actor uuid;
  normalized_project_key text;
  normalized_display_name text;
  request_sha text;
  project memory.project_space%ROWTYPE;
  prior_event memory.project_space_registration_event%ROWTYPE;
  outcome text;
BEGIN
  actor := memory.current_actor_user_id();
  normalized_project_key := btrim(p_project_key);
  normalized_display_name := btrim(p_display_name);
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_request_id IS NULL
     OR normalized_project_key IS NULL OR normalized_project_key = ''
     OR normalized_display_name IS NULL OR normalized_display_name = '' THEN
    RAISE EXCEPTION 'request_id, project_key, and display_name are required'
      USING ERRCODE = '22023';
  END IF;
  IF length(normalized_project_key) > 500
     OR length(normalized_display_name) > 500 THEN
    RAISE EXCEPTION 'project_key and display_name are limited to 500 characters'
      USING ERRCODE = '22023';
  END IF;
  IF p_metadata IS NULL OR jsonb_typeof(p_metadata) <> 'object'
     OR pg_column_size(p_metadata) > 16384 THEN
    RAISE EXCEPTION 'metadata must be a JSON object no larger than 16 KiB'
      USING ERRCODE = '22023';
  END IF;

  request_sha := memory.review_request_sha(jsonb_build_object(
    'operation', 'register_project_space_v1',
    'project_key', normalized_project_key,
    'display_name', normalized_display_name,
    'metadata', p_metadata
  ));

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':project_registration_request:' || p_request_id::text, 0
  ));
  SELECT stored.*
  INTO prior_event
  FROM memory.project_space_registration_event AS stored
  WHERE stored.owner_user_id = actor
    AND stored.request_id = p_request_id;
  IF FOUND THEN
    IF prior_event.request_sha256 <> request_sha THEN
      RAISE EXCEPTION 'request_id was reused with different project inputs'
        USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
      SELECT stored.*
      FROM memory.project_space AS stored
      WHERE stored.owner_user_id = actor
        AND stored.project_id = prior_event.project_id;
    RETURN;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':project_key:' || normalized_project_key, 0
  ));
  SELECT stored.*
  INTO project
  FROM memory.project_space AS stored
  WHERE stored.owner_user_id = actor
    AND stored.project_key = normalized_project_key;

  IF FOUND THEN
    IF project.display_name <> normalized_display_name
       OR project.metadata <> p_metadata THEN
      RAISE EXCEPTION
        'project_key already exists with different immutable registration data'
        USING ERRCODE = '23514';
    END IF;
    outcome := 'existing';
  ELSE
    INSERT INTO memory.project_space(
      owner_user_id, project_key, display_name, metadata
    ) VALUES (
      actor, normalized_project_key, normalized_display_name, p_metadata
    )
    RETURNING * INTO project;
    outcome := 'created';
  END IF;

  INSERT INTO memory.project_space_registration_event(
    owner_user_id, request_id, request_sha256, project_id, outcome,
    actor_user_id, invoked_by_role, metadata
  ) VALUES (
    actor, p_request_id, request_sha, project.project_id, outcome,
    actor, session_user, p_metadata
  );

  RETURN NEXT project;
END
$$;

CREATE OR REPLACE FUNCTION memory.review_preference_candidate(
  p_candidate_id uuid,
  p_request_id uuid,
  p_expected_candidate_hash text,
  p_decision text,
  p_reviewer_type text,
  p_reviewer_ref text,
  p_rationale text,
  p_reason_codes text[],
  p_replacement_candidate_ids uuid[] DEFAULT '{}'::uuid[],
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS SETOF memory.preference_candidate_review
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $$
DECLARE
  actor uuid;
  candidate memory.preference_candidate%ROWTYPE;
  replacement memory.preference_candidate%ROWTYPE;
  prior_review memory.preference_candidate_review%ROWTYPE;
  review memory.preference_candidate_review%ROWTYPE;
  replacement_id uuid;
  replacements uuid[];
  reason_codes text[];
  reviewer_ref text;
  request_sha text;
  next_review_number integer;
  total_links integer;
  active_links integer;
BEGIN
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_candidate_id IS NULL OR p_request_id IS NULL THEN
    RAISE EXCEPTION 'candidate_id and request_id are required'
      USING ERRCODE = '22023';
  END IF;
  IF p_expected_candidate_hash IS NULL
     OR p_expected_candidate_hash !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'expected_candidate_hash must be lowercase SHA-256 hex'
      USING ERRCODE = '22023';
  END IF;
  IF p_decision IS NULL
     OR p_decision NOT IN ('accept', 'rewrite', 'reject', 'defer', 'split') THEN
    RAISE EXCEPTION 'invalid review decision: %', p_decision
      USING ERRCODE = '22023';
  END IF;
  IF p_reviewer_type IS NULL
     OR p_reviewer_type NOT IN ('user', 'admin', 'job') THEN
    RAISE EXCEPTION 'invalid reviewer_type: %', p_reviewer_type
      USING ERRCODE = '22023';
  END IF;
  IF p_reviewer_type = 'job' AND p_decision IN ('accept', 'reject') THEN
    RAISE EXCEPTION 'jobs cannot accept or reject candidates'
      USING ERRCODE = '42501';
  END IF;
  IF p_rationale IS NULL OR btrim(p_rationale) = '' THEN
    RAISE EXCEPTION 'review rationale is required'
      USING ERRCODE = '22023';
  END IF;
  IF p_metadata IS NULL OR jsonb_typeof(p_metadata) <> 'object'
     OR pg_column_size(p_metadata) > 16384 THEN
    RAISE EXCEPTION 'metadata must be a JSON object no larger than 16 KiB'
      USING ERRCODE = '22023';
  END IF;

  IF EXISTS (
    SELECT 1 FROM unnest(coalesce(p_reason_codes, '{}'::text[])) AS item(code)
    WHERE code IS NULL OR btrim(code) = ''
  ) THEN
    RAISE EXCEPTION 'reason_codes cannot contain blank values'
      USING ERRCODE = '22023';
  END IF;
  SELECT coalesce(
    array_agg(DISTINCT btrim(code) ORDER BY btrim(code)), '{}'::text[]
  )
  INTO reason_codes
  FROM unnest(coalesce(p_reason_codes, '{}'::text[])) AS item(code);
  IF cardinality(reason_codes) = 0 THEN
    RAISE EXCEPTION 'at least one reason_code is required'
      USING ERRCODE = '22023';
  END IF;

  replacements := coalesce(p_replacement_candidate_ids, '{}'::uuid[]);
  IF array_position(replacements, NULL) IS NOT NULL
     OR (
       SELECT count(*) <> count(DISTINCT id)
       FROM unnest(replacements) AS item(id)
     ) THEN
    RAISE EXCEPTION 'replacement candidates must be non-null and unique'
      USING ERRCODE = '22023';
  END IF;
  IF p_decision = 'rewrite' AND cardinality(replacements) <> 1 THEN
    RAISE EXCEPTION 'rewrite requires exactly one replacement candidate'
      USING ERRCODE = '22023';
  END IF;
  IF p_decision = 'split' AND cardinality(replacements) < 2 THEN
    RAISE EXCEPTION 'split requires at least two replacement candidates'
      USING ERRCODE = '22023';
  END IF;
  IF p_decision IN ('accept', 'reject', 'defer')
     AND cardinality(replacements) <> 0 THEN
    RAISE EXCEPTION '% does not accept replacement candidates', p_decision
      USING ERRCODE = '22023';
  END IF;

  reviewer_ref := nullif(btrim(p_reviewer_ref), '');
  IF reviewer_ref IS NULL AND p_reviewer_type = 'user' THEN
    reviewer_ref := actor::text;
  END IF;
  IF reviewer_ref IS NULL THEN
    RAISE EXCEPTION 'reviewer_ref is required for admin and job reviews'
      USING ERRCODE = '22023';
  END IF;
  IF length(reviewer_ref) > 500 THEN
    RAISE EXCEPTION 'reviewer_ref exceeds 500 characters'
      USING ERRCODE = '22023';
  END IF;

  request_sha := memory.review_request_sha(jsonb_build_object(
    'operation', 'review_preference_candidate_v1',
    'candidate_id', p_candidate_id,
    'expected_candidate_hash', p_expected_candidate_hash,
    'decision', p_decision,
    'reviewer_type', p_reviewer_type,
    'reviewer_ref', reviewer_ref,
    'rationale', btrim(p_rationale),
    'reason_codes', to_jsonb(reason_codes),
    'replacement_candidate_ids', to_jsonb(replacements),
    'metadata', p_metadata
  ));

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':preference_review_request:' || p_request_id::text, 0
  ));
  SELECT stored.*
  INTO prior_review
  FROM memory.preference_candidate_review AS stored
  WHERE stored.owner_user_id = actor
    AND stored.request_id = p_request_id;
  IF FOUND THEN
    IF prior_review.request_sha256 <> request_sha THEN
      RAISE EXCEPTION 'request_id was reused with different review inputs'
        USING ERRCODE = '22023';
    END IF;
    RETURN NEXT prior_review;
    RETURN;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':preference_candidate:' || p_candidate_id::text, 0
  ));
  SELECT stored.*
  INTO candidate
  FROM memory.preference_candidate AS stored
  WHERE stored.owner_user_id = actor
    AND stored.candidate_id = p_candidate_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'preference candidate is not visible to the current actor'
      USING ERRCODE = 'P0002';
  END IF;
  IF candidate.candidate_hash <> p_expected_candidate_hash THEN
    RAISE EXCEPTION 'preference candidate hash changed'
      USING ERRCODE = '22023';
  END IF;

  SELECT count(*)::integer,
         count(*) FILTER (WHERE evidence.status = 'active')::integer
  INTO total_links, active_links
  FROM memory.preference_candidate_evidence AS link
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = link.owner_user_id
   AND evidence.evidence_id = link.evidence_id
  WHERE link.owner_user_id = actor
    AND link.candidate_id = p_candidate_id;
  IF total_links = 0 OR active_links <> total_links THEN
    RAISE EXCEPTION 'preference candidate lacks fully active evidence'
      USING ERRCODE = '23514';
  END IF;
  PERFORM 1
  FROM memory.preference_candidate_evidence AS link
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = link.owner_user_id
   AND evidence.evidence_id = link.evidence_id
  WHERE link.owner_user_id = actor
    AND link.candidate_id = p_candidate_id
  FOR KEY SHARE OF evidence;

  FOREACH replacement_id IN ARRAY replacements
  LOOP
    IF replacement_id = p_candidate_id THEN
      RAISE EXCEPTION 'a replacement cannot reference the source candidate'
        USING ERRCODE = '22023';
    END IF;
    SELECT stored.*
    INTO replacement
    FROM memory.preference_candidate AS stored
    WHERE stored.owner_user_id = actor
      AND stored.candidate_id = replacement_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'replacement preference candidate is not visible'
        USING ERRCODE = 'P0002';
    END IF;

    SELECT count(*)::integer,
           count(*) FILTER (WHERE evidence.status = 'active')::integer
    INTO total_links, active_links
    FROM memory.preference_candidate_evidence AS link
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id = link.owner_user_id
     AND evidence.evidence_id = link.evidence_id
    WHERE link.owner_user_id = actor
      AND link.candidate_id = replacement_id;
    IF total_links = 0 OR active_links <> total_links THEN
      RAISE EXCEPTION 'replacement preference candidate lacks fully active evidence'
        USING ERRCODE = '23514';
    END IF;
    PERFORM 1
    FROM memory.preference_candidate_evidence AS link
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id = link.owner_user_id
     AND evidence.evidence_id = link.evidence_id
    WHERE link.owner_user_id = actor
      AND link.candidate_id = replacement_id
    FOR KEY SHARE OF evidence;
  END LOOP;

  SELECT coalesce(max(stored.review_number), 0) + 1
  INTO next_review_number
  FROM memory.preference_candidate_review AS stored
  WHERE stored.owner_user_id = actor
    AND stored.candidate_id = p_candidate_id;

  INSERT INTO memory.preference_candidate_review(
    owner_user_id, candidate_id, review_number, decision,
    expected_candidate_hash, reviewer_type, reviewer_ref, rationale,
    reason_codes, metadata, request_id, request_sha256
  ) VALUES (
    actor, p_candidate_id, next_review_number, p_decision,
    p_expected_candidate_hash, p_reviewer_type, reviewer_ref, btrim(p_rationale),
    reason_codes, p_metadata, p_request_id, request_sha
  )
  RETURNING * INTO review;

  INSERT INTO memory.preference_candidate_review_replacement(
    owner_user_id, review_id, replacement_candidate_id, ordinal
  )
  SELECT actor, review.review_id, item.id, item.ordinality::integer
  FROM unnest(replacements) WITH ORDINALITY AS item(id, ordinality);

  RETURN NEXT review;
END
$$;

CREATE OR REPLACE FUNCTION memory.review_project_candidate(
  p_candidate_id uuid,
  p_request_id uuid,
  p_expected_candidate_hash text,
  p_decision text,
  p_reviewer_type text,
  p_reviewer_ref text,
  p_rationale text,
  p_reason_codes text[],
  p_replacement_candidate_ids uuid[] DEFAULT '{}'::uuid[],
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS SETOF memory.project_knowledge_candidate_review
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $$
DECLARE
  actor uuid;
  candidate memory.project_knowledge_candidate%ROWTYPE;
  replacement memory.project_knowledge_candidate%ROWTYPE;
  prior_review memory.project_knowledge_candidate_review%ROWTYPE;
  review memory.project_knowledge_candidate_review%ROWTYPE;
  replacement_id uuid;
  replacements uuid[];
  reason_codes text[];
  reviewer_ref text;
  request_sha text;
  next_review_number integer;
  total_links integer;
  active_links integer;
BEGIN
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_candidate_id IS NULL OR p_request_id IS NULL THEN
    RAISE EXCEPTION 'candidate_id and request_id are required'
      USING ERRCODE = '22023';
  END IF;
  IF p_expected_candidate_hash IS NULL
     OR p_expected_candidate_hash !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'expected_candidate_hash must be lowercase SHA-256 hex'
      USING ERRCODE = '22023';
  END IF;
  IF p_decision IS NULL
     OR p_decision NOT IN ('accept', 'rewrite', 'reject', 'defer', 'split') THEN
    RAISE EXCEPTION 'invalid review decision: %', p_decision
      USING ERRCODE = '22023';
  END IF;
  IF p_reviewer_type IS NULL
     OR p_reviewer_type NOT IN ('user', 'admin', 'job') THEN
    RAISE EXCEPTION 'invalid reviewer_type: %', p_reviewer_type
      USING ERRCODE = '22023';
  END IF;
  IF p_reviewer_type = 'job' AND p_decision IN ('accept', 'reject') THEN
    RAISE EXCEPTION 'jobs cannot accept or reject candidates'
      USING ERRCODE = '42501';
  END IF;
  IF p_rationale IS NULL OR btrim(p_rationale) = '' THEN
    RAISE EXCEPTION 'review rationale is required'
      USING ERRCODE = '22023';
  END IF;
  IF p_metadata IS NULL OR jsonb_typeof(p_metadata) <> 'object'
     OR pg_column_size(p_metadata) > 16384 THEN
    RAISE EXCEPTION 'metadata must be a JSON object no larger than 16 KiB'
      USING ERRCODE = '22023';
  END IF;

  IF EXISTS (
    SELECT 1 FROM unnest(coalesce(p_reason_codes, '{}'::text[])) AS item(code)
    WHERE code IS NULL OR btrim(code) = ''
  ) THEN
    RAISE EXCEPTION 'reason_codes cannot contain blank values'
      USING ERRCODE = '22023';
  END IF;
  SELECT coalesce(
    array_agg(DISTINCT btrim(code) ORDER BY btrim(code)), '{}'::text[]
  )
  INTO reason_codes
  FROM unnest(coalesce(p_reason_codes, '{}'::text[])) AS item(code);
  IF cardinality(reason_codes) = 0 THEN
    RAISE EXCEPTION 'at least one reason_code is required'
      USING ERRCODE = '22023';
  END IF;

  replacements := coalesce(p_replacement_candidate_ids, '{}'::uuid[]);
  IF array_position(replacements, NULL) IS NOT NULL
     OR (
       SELECT count(*) <> count(DISTINCT id)
       FROM unnest(replacements) AS item(id)
     ) THEN
    RAISE EXCEPTION 'replacement candidates must be non-null and unique'
      USING ERRCODE = '22023';
  END IF;
  IF p_decision = 'rewrite' AND cardinality(replacements) <> 1 THEN
    RAISE EXCEPTION 'rewrite requires exactly one replacement candidate'
      USING ERRCODE = '22023';
  END IF;
  IF p_decision = 'split' AND cardinality(replacements) < 2 THEN
    RAISE EXCEPTION 'split requires at least two replacement candidates'
      USING ERRCODE = '22023';
  END IF;
  IF p_decision IN ('accept', 'reject', 'defer')
     AND cardinality(replacements) <> 0 THEN
    RAISE EXCEPTION '% does not accept replacement candidates', p_decision
      USING ERRCODE = '22023';
  END IF;

  reviewer_ref := nullif(btrim(p_reviewer_ref), '');
  IF reviewer_ref IS NULL AND p_reviewer_type = 'user' THEN
    reviewer_ref := actor::text;
  END IF;
  IF reviewer_ref IS NULL THEN
    RAISE EXCEPTION 'reviewer_ref is required for admin and job reviews'
      USING ERRCODE = '22023';
  END IF;
  IF length(reviewer_ref) > 500 THEN
    RAISE EXCEPTION 'reviewer_ref exceeds 500 characters'
      USING ERRCODE = '22023';
  END IF;

  request_sha := memory.review_request_sha(jsonb_build_object(
    'operation', 'review_project_candidate_v1',
    'candidate_id', p_candidate_id,
    'expected_candidate_hash', p_expected_candidate_hash,
    'decision', p_decision,
    'reviewer_type', p_reviewer_type,
    'reviewer_ref', reviewer_ref,
    'rationale', btrim(p_rationale),
    'reason_codes', to_jsonb(reason_codes),
    'replacement_candidate_ids', to_jsonb(replacements),
    'metadata', p_metadata
  ));

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':project_review_request:' || p_request_id::text, 0
  ));
  SELECT stored.*
  INTO prior_review
  FROM memory.project_knowledge_candidate_review AS stored
  WHERE stored.owner_user_id = actor
    AND stored.request_id = p_request_id;
  IF FOUND THEN
    IF prior_review.request_sha256 <> request_sha THEN
      RAISE EXCEPTION 'request_id was reused with different review inputs'
        USING ERRCODE = '22023';
    END IF;
    RETURN NEXT prior_review;
    RETURN;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':project_candidate:' || p_candidate_id::text, 0
  ));
  SELECT stored.*
  INTO candidate
  FROM memory.project_knowledge_candidate AS stored
  WHERE stored.owner_user_id = actor
    AND stored.candidate_id = p_candidate_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'project candidate is not visible to the current actor'
      USING ERRCODE = 'P0002';
  END IF;
  IF candidate.candidate_hash <> p_expected_candidate_hash THEN
    RAISE EXCEPTION 'project candidate hash changed'
      USING ERRCODE = '22023';
  END IF;
  IF p_decision = 'accept'
     AND (
       candidate.document_state = 'unverified'
       OR candidate.authority_level = 'unverified'
     ) THEN
    RAISE EXCEPTION 'unverified project knowledge cannot be accepted'
      USING ERRCODE = '23514';
  END IF;

  SELECT count(*)::integer,
         count(*) FILTER (WHERE evidence.status = 'active')::integer
  INTO total_links, active_links
  FROM memory.project_knowledge_candidate_evidence AS link
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = link.owner_user_id
   AND evidence.evidence_id = link.evidence_id
  WHERE link.owner_user_id = actor
    AND link.project_id = candidate.project_id
    AND link.candidate_id = p_candidate_id;
  IF total_links = 0 OR active_links <> total_links THEN
    RAISE EXCEPTION 'project candidate lacks fully active evidence'
      USING ERRCODE = '23514';
  END IF;
  PERFORM 1
  FROM memory.project_knowledge_candidate_evidence AS link
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = link.owner_user_id
   AND evidence.evidence_id = link.evidence_id
  WHERE link.owner_user_id = actor
    AND link.project_id = candidate.project_id
    AND link.candidate_id = p_candidate_id
  FOR KEY SHARE OF evidence;

  FOREACH replacement_id IN ARRAY replacements
  LOOP
    IF replacement_id = p_candidate_id THEN
      RAISE EXCEPTION 'a replacement cannot reference the source candidate'
        USING ERRCODE = '22023';
    END IF;
    SELECT stored.*
    INTO replacement
    FROM memory.project_knowledge_candidate AS stored
    WHERE stored.owner_user_id = actor
      AND stored.candidate_id = replacement_id;
    IF NOT FOUND OR replacement.project_id <> candidate.project_id THEN
      RAISE EXCEPTION 'replacement project candidate is not in the same project'
        USING ERRCODE = 'P0002';
    END IF;

    SELECT count(*)::integer,
           count(*) FILTER (WHERE evidence.status = 'active')::integer
    INTO total_links, active_links
    FROM memory.project_knowledge_candidate_evidence AS link
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id = link.owner_user_id
     AND evidence.evidence_id = link.evidence_id
    WHERE link.owner_user_id = actor
      AND link.project_id = candidate.project_id
      AND link.candidate_id = replacement_id;
    IF total_links = 0 OR active_links <> total_links THEN
      RAISE EXCEPTION 'replacement project candidate lacks fully active evidence'
        USING ERRCODE = '23514';
    END IF;
    PERFORM 1
    FROM memory.project_knowledge_candidate_evidence AS link
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id = link.owner_user_id
     AND evidence.evidence_id = link.evidence_id
    WHERE link.owner_user_id = actor
      AND link.project_id = candidate.project_id
      AND link.candidate_id = replacement_id
    FOR KEY SHARE OF evidence;
  END LOOP;

  SELECT coalesce(max(stored.review_number), 0) + 1
  INTO next_review_number
  FROM memory.project_knowledge_candidate_review AS stored
  WHERE stored.owner_user_id = actor
    AND stored.project_id = candidate.project_id
    AND stored.candidate_id = p_candidate_id;

  INSERT INTO memory.project_knowledge_candidate_review(
    owner_user_id, project_id, candidate_id, review_number, decision,
    expected_candidate_hash, reviewer_type, reviewer_ref, rationale,
    reason_codes, metadata, request_id, request_sha256
  ) VALUES (
    actor, candidate.project_id, p_candidate_id, next_review_number, p_decision,
    p_expected_candidate_hash, p_reviewer_type, reviewer_ref, btrim(p_rationale),
    reason_codes, p_metadata, p_request_id, request_sha
  )
  RETURNING * INTO review;

  INSERT INTO memory.project_knowledge_candidate_review_replacement(
    owner_user_id, project_id, review_id, replacement_candidate_id, ordinal
  )
  SELECT actor, candidate.project_id, review.review_id,
         item.id, item.ordinality::integer
  FROM unnest(replacements) WITH ORDINALITY AS item(id, ordinality);

  RETURN NEXT review;
END
$$;

CREATE OR REPLACE FUNCTION memory.apply_preference_candidate(
  p_accepted_review_id uuid,
  p_request_id uuid,
  p_expected_current_revision_id uuid,
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS SETOF memory.preference_apply_event
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $$
DECLARE
  actor uuid;
  request_sha text;
  review memory.preference_candidate_review%ROWTYPE;
  candidate memory.preference_candidate%ROWTYPE;
  head memory.user_preference%ROWTYPE;
  revision memory.preference_revision%ROWTYPE;
  prior_event memory.preference_apply_event%ROWTYPE;
  event memory.preference_apply_event%ROWTYPE;
  prior_revision_id uuid;
  primary_evidence_id uuid;
  next_revision_number integer;
  total_links integer;
  active_links integer;
BEGIN
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_accepted_review_id IS NULL OR p_request_id IS NULL THEN
    RAISE EXCEPTION 'accepted_review_id and request_id are required'
      USING ERRCODE = '22023';
  END IF;
  IF p_metadata IS NULL OR jsonb_typeof(p_metadata) <> 'object'
     OR pg_column_size(p_metadata) > 16384 THEN
    RAISE EXCEPTION 'metadata must be a JSON object no larger than 16 KiB'
      USING ERRCODE = '22023';
  END IF;

  request_sha := memory.review_request_sha(jsonb_build_object(
    'operation', 'apply_preference_candidate_v1',
    'accepted_review_id', p_accepted_review_id,
    'expected_current_revision_id', p_expected_current_revision_id,
    'metadata', p_metadata
  ));

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':preference_apply_request:' || p_request_id::text, 0
  ));
  SELECT stored.*
  INTO prior_event
  FROM memory.preference_apply_event AS stored
  WHERE stored.owner_user_id = actor
    AND stored.request_id = p_request_id;
  IF FOUND THEN
    IF prior_event.request_sha256 <> request_sha THEN
      RAISE EXCEPTION 'request_id was reused with different preference apply inputs'
        USING ERRCODE = '22023';
    END IF;
    RETURN NEXT prior_event;
    RETURN;
  END IF;

  SELECT stored.*
  INTO review
  FROM memory.preference_candidate_review AS stored
  WHERE stored.owner_user_id = actor
    AND stored.review_id = p_accepted_review_id
    AND stored.decision = 'accept'
    AND NOT EXISTS (
      SELECT 1
      FROM memory.preference_candidate_review AS newer
      WHERE newer.owner_user_id = stored.owner_user_id
        AND newer.candidate_id = stored.candidate_id
        AND newer.review_number > stored.review_number
    );
  IF NOT FOUND THEN
    RAISE EXCEPTION 'review is not the latest visible acceptance'
      USING ERRCODE = '23514';
  END IF;

  SELECT stored.*
  INTO candidate
  FROM memory.preference_candidate AS stored
  WHERE stored.owner_user_id = actor
    AND stored.candidate_id = review.candidate_id
    AND stored.candidate_hash = review.expected_candidate_hash;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'accepted preference candidate/hash is not visible'
      USING ERRCODE = 'P0002';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':preference_key:' || candidate.preference_key, 0
  ));
  IF EXISTS (
    SELECT 1
    FROM memory.preference_apply_event AS stored
    WHERE stored.owner_user_id = actor
      AND stored.accepted_review_id = p_accepted_review_id
  ) THEN
    RAISE EXCEPTION 'accepted preference review was already applied'
      USING ERRCODE = '23505';
  END IF;

  SELECT count(*)::integer,
         count(*) FILTER (WHERE evidence.status = 'active')::integer
  INTO total_links, active_links
  FROM memory.preference_candidate_evidence AS link
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = link.owner_user_id
   AND evidence.evidence_id = link.evidence_id
  WHERE link.owner_user_id = actor
    AND link.candidate_id = candidate.candidate_id;
  IF total_links = 0 OR active_links <> total_links THEN
    RAISE EXCEPTION 'preference apply requires fully active evidence'
      USING ERRCODE = '23514';
  END IF;
  SELECT link.evidence_id
  INTO primary_evidence_id
  FROM memory.preference_candidate_evidence AS link
  WHERE link.owner_user_id = actor
    AND link.candidate_id = candidate.candidate_id
  ORDER BY link.evidence_id
  LIMIT 1;
  PERFORM 1
  FROM memory.preference_candidate_evidence AS link
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = link.owner_user_id
   AND evidence.evidence_id = link.evidence_id
  WHERE link.owner_user_id = actor
    AND link.candidate_id = candidate.candidate_id
  FOR KEY SHARE OF evidence;

  SELECT stored.*
  INTO head
  FROM memory.user_preference AS stored
  WHERE stored.owner_user_id = actor
    AND stored.preference_key = candidate.preference_key
  FOR UPDATE;

  IF FOUND THEN
    IF head.current_revision_id IS DISTINCT FROM p_expected_current_revision_id THEN
      RAISE EXCEPTION 'stale expected preference revision'
        USING ERRCODE = '40001';
    END IF;
    prior_revision_id := head.current_revision_id;
    next_revision_number := head.revision_number + 1;
  ELSE
    IF p_expected_current_revision_id IS NOT NULL THEN
      RAISE EXCEPTION 'stale expected preference revision'
        USING ERRCODE = '40001';
    END IF;
    INSERT INTO memory.user_preference(
      owner_user_id, preference_key, value, status, explicit, confidence,
      evidence_id, preference_class, preference_domain, polarity, scope,
      stability, surface_policy, sensitivity
    ) VALUES (
      actor, candidate.preference_key, candidate.value, 'active',
      candidate.explicit, candidate.extraction_confidence, primary_evidence_id,
      candidate.preference_class, candidate.preference_domain,
      candidate.polarity, candidate.scope, candidate.stability,
      candidate.surface_policy, candidate.sensitivity
    )
    RETURNING * INTO head;
    prior_revision_id := NULL;
    next_revision_number := 1;
  END IF;

  INSERT INTO memory.preference_revision(
    owner_user_id, preference_id, candidate_id, accepted_review_id,
    revision_number, preference_class, preference_domain, preference_key,
    value, polarity, scope, explicit, stability, surface_policy,
    extraction_confidence, sensitivity, content_sha256, prior_revision_id,
    metadata
  ) VALUES (
    actor, head.preference_id, candidate.candidate_id, review.review_id,
    next_revision_number, candidate.preference_class,
    candidate.preference_domain, candidate.preference_key, candidate.value,
    candidate.polarity, candidate.scope, candidate.explicit,
    candidate.stability, candidate.surface_policy,
    candidate.extraction_confidence, candidate.sensitivity,
    candidate.candidate_hash, prior_revision_id, candidate.metadata
  )
  RETURNING * INTO revision;

  INSERT INTO memory.preference_revision_evidence(
    owner_user_id, revision_id, evidence_id, stance, relevance, rationale
  )
  SELECT link.owner_user_id, revision.revision_id, link.evidence_id,
         link.stance, link.relevance, link.rationale
  FROM memory.preference_candidate_evidence AS link
  WHERE link.owner_user_id = actor
    AND link.candidate_id = candidate.candidate_id
  ORDER BY link.evidence_id;

  UPDATE memory.user_preference
  SET preference_class = candidate.preference_class,
      preference_domain = candidate.preference_domain,
      value = candidate.value,
      status = 'active',
      explicit = candidate.explicit,
      confidence = candidate.extraction_confidence,
      evidence_id = primary_evidence_id,
      polarity = candidate.polarity,
      scope = candidate.scope,
      stability = candidate.stability,
      surface_policy = candidate.surface_policy,
      sensitivity = candidate.sensitivity,
      current_revision_id = revision.revision_id,
      revision_number = revision.revision_number,
      content_sha256 = candidate.candidate_hash,
      accepted_review_id = review.review_id
  WHERE owner_user_id = actor
    AND preference_id = head.preference_id;

  INSERT INTO memory.preference_apply_event(
    owner_user_id, request_id, request_sha256, candidate_id,
    accepted_review_id, preference_id, prior_revision_id,
    resulting_revision_id, actor_user_id, invoked_by_role, metadata
  ) VALUES (
    actor, p_request_id, request_sha, candidate.candidate_id,
    review.review_id, head.preference_id, prior_revision_id,
    revision.revision_id, actor, session_user, p_metadata
  )
  RETURNING * INTO event;

  RETURN NEXT event;
END
$$;

CREATE OR REPLACE FUNCTION memory.apply_project_candidate(
  p_accepted_review_id uuid,
  p_request_id uuid,
  p_expected_current_revision_id uuid,
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS SETOF memory.project_knowledge_apply_event
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $$
DECLARE
  actor uuid;
  request_sha text;
  review memory.project_knowledge_candidate_review%ROWTYPE;
  candidate memory.project_knowledge_candidate%ROWTYPE;
  head memory.project_knowledge_head%ROWTYPE;
  current_revision memory.project_knowledge_revision%ROWTYPE;
  revision memory.project_knowledge_revision%ROWTYPE;
  prior_event memory.project_knowledge_apply_event%ROWTYPE;
  event memory.project_knowledge_apply_event%ROWTYPE;
  prior_revision_id uuid;
  next_revision_number integer;
  total_links integer;
  active_links integer;
BEGIN
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_accepted_review_id IS NULL OR p_request_id IS NULL THEN
    RAISE EXCEPTION 'accepted_review_id and request_id are required'
      USING ERRCODE = '22023';
  END IF;
  IF p_metadata IS NULL OR jsonb_typeof(p_metadata) <> 'object'
     OR pg_column_size(p_metadata) > 16384 THEN
    RAISE EXCEPTION 'metadata must be a JSON object no larger than 16 KiB'
      USING ERRCODE = '22023';
  END IF;

  request_sha := memory.review_request_sha(jsonb_build_object(
    'operation', 'apply_project_candidate_v1',
    'accepted_review_id', p_accepted_review_id,
    'expected_current_revision_id', p_expected_current_revision_id,
    'metadata', p_metadata
  ));

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':project_apply_request:' || p_request_id::text, 0
  ));
  SELECT stored.*
  INTO prior_event
  FROM memory.project_knowledge_apply_event AS stored
  WHERE stored.owner_user_id = actor
    AND stored.request_id = p_request_id;
  IF FOUND THEN
    IF prior_event.request_sha256 <> request_sha THEN
      RAISE EXCEPTION 'request_id was reused with different project apply inputs'
        USING ERRCODE = '22023';
    END IF;
    RETURN NEXT prior_event;
    RETURN;
  END IF;

  SELECT stored.*
  INTO review
  FROM memory.project_knowledge_candidate_review AS stored
  WHERE stored.owner_user_id = actor
    AND stored.review_id = p_accepted_review_id
    AND stored.decision = 'accept'
    AND NOT EXISTS (
      SELECT 1
      FROM memory.project_knowledge_candidate_review AS newer
      WHERE newer.owner_user_id = stored.owner_user_id
        AND newer.project_id = stored.project_id
        AND newer.candidate_id = stored.candidate_id
        AND newer.review_number > stored.review_number
    );
  IF NOT FOUND THEN
    RAISE EXCEPTION 'review is not the latest visible acceptance'
      USING ERRCODE = '23514';
  END IF;

  SELECT stored.*
  INTO candidate
  FROM memory.project_knowledge_candidate AS stored
  WHERE stored.owner_user_id = actor
    AND stored.project_id = review.project_id
    AND stored.candidate_id = review.candidate_id
    AND stored.candidate_hash = review.expected_candidate_hash;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'accepted project candidate/hash is not visible'
      USING ERRCODE = 'P0002';
  END IF;
  IF candidate.document_state = 'unverified'
     OR candidate.authority_level = 'unverified' THEN
    RAISE EXCEPTION 'unverified project knowledge cannot be applied'
      USING ERRCODE = '23514';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':project_knowledge_key:' || candidate.project_id::text
      || ':' || candidate.knowledge_key, 0
  ));
  IF EXISTS (
    SELECT 1
    FROM memory.project_knowledge_apply_event AS stored
    WHERE stored.owner_user_id = actor
      AND stored.project_id = candidate.project_id
      AND stored.accepted_review_id = p_accepted_review_id
  ) THEN
    RAISE EXCEPTION 'accepted project review was already applied'
      USING ERRCODE = '23505';
  END IF;

  SELECT count(*)::integer,
         count(*) FILTER (WHERE evidence.status = 'active')::integer
  INTO total_links, active_links
  FROM memory.project_knowledge_candidate_evidence AS link
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = link.owner_user_id
   AND evidence.evidence_id = link.evidence_id
  WHERE link.owner_user_id = actor
    AND link.project_id = candidate.project_id
    AND link.candidate_id = candidate.candidate_id;
  IF total_links = 0 OR active_links <> total_links THEN
    RAISE EXCEPTION 'project apply requires fully active evidence'
      USING ERRCODE = '23514';
  END IF;
  PERFORM 1
  FROM memory.project_knowledge_candidate_evidence AS link
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = link.owner_user_id
   AND evidence.evidence_id = link.evidence_id
  WHERE link.owner_user_id = actor
    AND link.project_id = candidate.project_id
    AND link.candidate_id = candidate.candidate_id
  FOR KEY SHARE OF evidence;

  SELECT stored.*
  INTO head
  FROM memory.project_knowledge_head AS stored
  WHERE stored.owner_user_id = actor
    AND stored.project_id = candidate.project_id
    AND stored.knowledge_key = candidate.knowledge_key;

  IF FOUND THEN
    IF head.knowledge_kind <> candidate.knowledge_kind THEN
      RAISE EXCEPTION 'project knowledge key cannot change kind'
        USING ERRCODE = '23514';
    END IF;
    SELECT stored.*
    INTO current_revision
    FROM memory.project_knowledge_revision AS stored
    WHERE stored.owner_user_id = actor
      AND stored.project_id = candidate.project_id
      AND stored.knowledge_id = head.knowledge_id
    ORDER BY stored.revision_number DESC
    LIMIT 1;
    IF NOT FOUND
       OR current_revision.revision_id IS DISTINCT FROM p_expected_current_revision_id THEN
      RAISE EXCEPTION 'stale expected project revision'
        USING ERRCODE = '40001';
    END IF;
    prior_revision_id := current_revision.revision_id;
    next_revision_number := current_revision.revision_number + 1;
  ELSE
    IF p_expected_current_revision_id IS NOT NULL THEN
      RAISE EXCEPTION 'stale expected project revision'
        USING ERRCODE = '40001';
    END IF;
    INSERT INTO memory.project_knowledge_head(
      owner_user_id, project_id, knowledge_key, knowledge_kind
    ) VALUES (
      actor, candidate.project_id, candidate.knowledge_key,
      candidate.knowledge_kind
    )
    RETURNING * INTO head;
    prior_revision_id := NULL;
    next_revision_number := 1;
  END IF;

  INSERT INTO memory.project_knowledge_revision(
    owner_user_id, project_id, knowledge_id, revision_number,
    canonical_text, content_sha256, document_state, authority_level,
    authority_source, effective_from, effective_to, accepted_review_id,
    supersedes_revision_id, sensitivity, metadata
  ) VALUES (
    actor, candidate.project_id, head.knowledge_id, next_revision_number,
    candidate.canonical_text, candidate.candidate_hash,
    candidate.document_state, candidate.authority_level,
    candidate.authority_source, candidate.effective_at, candidate.expires_at,
    review.review_id, prior_revision_id, candidate.sensitivity,
    candidate.metadata
  )
  RETURNING * INTO revision;

  INSERT INTO memory.project_knowledge_revision_evidence(
    owner_user_id, project_id, revision_id, evidence_id,
    stance, relevance, rationale
  )
  SELECT link.owner_user_id, link.project_id, revision.revision_id,
         link.evidence_id, link.stance, link.relevance, link.rationale
  FROM memory.project_knowledge_candidate_evidence AS link
  WHERE link.owner_user_id = actor
    AND link.project_id = candidate.project_id
    AND link.candidate_id = candidate.candidate_id
  ORDER BY link.evidence_id;

  INSERT INTO memory.project_knowledge_apply_event(
    owner_user_id, project_id, request_id, request_sha256, candidate_id,
    accepted_review_id, knowledge_id, prior_revision_id,
    resulting_revision_id, actor_user_id, invoked_by_role, metadata
  ) VALUES (
    actor, candidate.project_id, p_request_id, request_sha,
    candidate.candidate_id, review.review_id, head.knowledge_id,
    prior_revision_id, revision.revision_id, actor, session_user, p_metadata
  )
  RETURNING * INTO event;

  RETURN NEXT event;
END
$$;

DO $rls$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'preference_revision',
    'preference_revision_evidence',
    'preference_apply_event',
    'project_space_registration_event',
    'project_knowledge_apply_event'
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
  memory.preference_revision,
  memory.preference_revision_evidence,
  memory.preference_apply_event,
  memory.project_space_registration_event,
  memory.project_knowledge_apply_event
FROM PUBLIC, brains_app;

GRANT SELECT ON
  memory.preference_revision,
  memory.preference_revision_evidence,
  memory.preference_apply_event,
  memory.project_space_registration_event,
  memory.project_knowledge_apply_event
TO brains_app;

REVOKE INSERT, UPDATE, DELETE ON
  memory.preference_candidate_review,
  memory.preference_candidate_review_replacement,
  memory.project_space,
  memory.project_knowledge_candidate_review,
  memory.project_knowledge_candidate_review_replacement,
  memory.project_knowledge_head,
  memory.project_knowledge_revision,
  memory.project_knowledge_revision_evidence,
  memory.user_preference
FROM brains_app;

GRANT SELECT ON
  memory.preference_candidate_review,
  memory.preference_candidate_review_replacement,
  memory.project_space,
  memory.project_knowledge_candidate_review,
  memory.project_knowledge_candidate_review_replacement,
  memory.project_knowledge_head,
  memory.project_knowledge_revision,
  memory.project_knowledge_revision_evidence,
  memory.user_preference
TO brains_app;

GRANT SELECT ON
  memory.preference_candidate,
  memory.preference_candidate_evidence,
  memory.project_knowledge_candidate,
  memory.project_knowledge_candidate_evidence,
  memory.evidence
TO memory_review_maintainer;

GRANT SELECT, INSERT ON
  memory.preference_candidate_review,
  memory.preference_candidate_review_replacement,
  memory.project_space,
  memory.project_knowledge_candidate_review,
  memory.project_knowledge_candidate_review_replacement,
  memory.project_knowledge_head,
  memory.project_knowledge_revision,
  memory.project_knowledge_revision_evidence,
  memory.preference_revision,
  memory.preference_revision_evidence,
  memory.preference_apply_event,
  memory.project_space_registration_event,
  memory.project_knowledge_apply_event
TO memory_review_maintainer;

GRANT SELECT, INSERT, UPDATE ON memory.user_preference
  TO memory_review_maintainer;
GRANT SELECT, UPDATE ON memory.evidence
  TO memory_review_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_review_maintainer;

GRANT CREATE ON SCHEMA memory TO memory_review_maintainer;
ALTER FUNCTION memory.review_request_sha(jsonb)
  OWNER TO memory_review_maintainer;
ALTER FUNCTION memory.guard_user_preference_controlled_write()
  OWNER TO memory_review_maintainer;
ALTER FUNCTION memory.guard_preference_revision_insert()
  OWNER TO memory_review_maintainer;
ALTER FUNCTION memory.register_project_space(uuid,text,text,jsonb)
  OWNER TO memory_review_maintainer;
ALTER FUNCTION memory.review_preference_candidate(
  uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb
) OWNER TO memory_review_maintainer;
ALTER FUNCTION memory.review_project_candidate(
  uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb
) OWNER TO memory_review_maintainer;
ALTER FUNCTION memory.apply_preference_candidate(uuid,uuid,uuid,jsonb)
  OWNER TO memory_review_maintainer;
ALTER FUNCTION memory.apply_project_candidate(uuid,uuid,uuid,jsonb)
  OWNER TO memory_review_maintainer;
REVOKE CREATE ON SCHEMA memory FROM memory_review_maintainer;

REVOKE ALL ON FUNCTION memory.review_request_sha(jsonb) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_user_preference_controlled_write()
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_preference_revision_insert()
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.register_project_space(uuid,text,text,jsonb)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.review_preference_candidate(
  uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.review_project_candidate(
  uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.apply_preference_candidate(uuid,uuid,uuid,jsonb)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.apply_project_candidate(uuid,uuid,uuid,jsonb)
  FROM PUBLIC, brains_app;

GRANT EXECUTE ON FUNCTION memory.register_project_space(uuid,text,text,jsonb)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.review_preference_candidate(
  uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.review_project_candidate(
  uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.apply_preference_candidate(
  uuid,uuid,uuid,jsonb
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.apply_project_candidate(
  uuid,uuid,uuid,jsonb
) TO brains_app;

COMMIT;
