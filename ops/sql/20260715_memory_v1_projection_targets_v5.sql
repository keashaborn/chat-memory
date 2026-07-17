BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 projection targets V5 migration must run as sage, current_user=%',
      current_user;
  END IF;
  IF NOT EXISTS (
       SELECT 1 FROM pg_roles
       WHERE rolname = 'memory_v5_writer'
         AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
         AND NOT rolcreaterole AND NOT rolinherit AND NOT rolbypassrls
     ) THEN
    RAISE EXCEPTION 'restricted memory_v5_writer role is required';
  END IF;
  IF to_regclass('memory.project_space') IS NULL
     OR to_regclass('memory.observation') IS NULL
     OR to_regclass('memory.observation_temporal') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('memory.guard_v5_append_only()') IS NULL
     OR to_regprocedure('memory.v5_sha256_valid(text)') IS NULL THEN
    RAISE EXCEPTION
      'V5 relational writer and owner-scoped project registry are required';
  END IF;
END
$block$;

CREATE TABLE IF NOT EXISTS memory.preference_head_v5 (
  preference_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  semantic_key_sha256 text NOT NULL,
  preference_class text NOT NULL,
  preference_domain text NOT NULL,
  preference_key text NOT NULL,
  scope text NOT NULL,
  status memory.record_status NOT NULL DEFAULT 'active',
  current_revision_id uuid,
  revision_number integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, preference_id),
  UNIQUE (owner_user_id, semantic_key_sha256),
  UNIQUE (
    owner_user_id, preference_class, preference_domain, preference_key, scope
  ),
  CHECK (memory.v5_sha256_valid(semantic_key_sha256)),
  CHECK (preference_class IN ('life', 'response')),
  CHECK (preference_domain ~ '^[a-z][a-z0-9_]{1,63}$'),
  CHECK (preference_key ~ '^[a-z][a-z0-9_.:-]{1,239}$'),
  CHECK (scope = 'user_global'),
  CHECK (
    (current_revision_id IS NULL AND revision_number = 0)
    OR (current_revision_id IS NOT NULL AND revision_number > 0)
  ),
  CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS memory.preference_revision_v5 (
  revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  preference_id uuid NOT NULL,
  revision_number integer NOT NULL,
  value jsonb NOT NULL,
  preference_polarity text NOT NULL,
  stability text NOT NULL,
  surface_policy memory.observation_surface_policy NOT NULL,
  content_sha256 text NOT NULL,
  prior_revision_id uuid,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, revision_id),
  UNIQUE (owner_user_id, preference_id, revision_id),
  UNIQUE (owner_user_id, preference_id, revision_number),
  UNIQUE (owner_user_id, preference_id, content_sha256),
  FOREIGN KEY (owner_user_id, preference_id)
    REFERENCES memory.preference_head_v5(owner_user_id, preference_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, prior_revision_id)
    REFERENCES memory.preference_revision_v5(owner_user_id, revision_id)
    ON DELETE RESTRICT,
  CHECK (revision_number > 0),
  CHECK (pg_column_size(value) <= 16384),
  CHECK (preference_polarity IN (
    'likes', 'dislikes', 'prefers', 'avoids', 'not_applicable'
  )),
  CHECK (stability IN ('tentative', 'contextual', 'stable')),
  CHECK (memory.v5_sha256_valid(content_sha256)),
  CHECK (prior_revision_id IS NULL OR prior_revision_id <> revision_id),
  CHECK (jsonb_typeof(metadata) = 'object' AND pg_column_size(metadata) <= 16384),
  CHECK (
    (preference_polarity = 'not_applicable'
     AND surface_policy = 'zero_token_control_only')
    OR
    (preference_polarity <> 'not_applicable'
     AND surface_policy IN (
       'relevant_recommendation_or_explicit_recall', 'never'
     ))
  )
);

DO $block$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.preference_head_v5'::regclass
      AND conname = 'preference_head_v5_current_revision_fk'
  ) THEN
    ALTER TABLE memory.preference_head_v5
      ADD CONSTRAINT preference_head_v5_current_revision_fk
      FOREIGN KEY (owner_user_id, preference_id, current_revision_id)
      REFERENCES memory.preference_revision_v5(
        owner_user_id, preference_id, revision_id
      ) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
  END IF;
END
$block$;

CREATE TABLE IF NOT EXISTS memory.project_knowledge_head_v5 (
  knowledge_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  component_key text,
  binding_source text NOT NULL,
  semantic_key_sha256 text NOT NULL,
  knowledge_kind text NOT NULL,
  knowledge_key text NOT NULL,
  status memory.record_status NOT NULL DEFAULT 'active',
  current_revision_id uuid,
  revision_number integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, project_id, knowledge_id),
  UNIQUE (owner_user_id, project_id, semantic_key_sha256),
  FOREIGN KEY (owner_user_id, project_id)
    REFERENCES memory.project_space(owner_user_id, project_id)
    ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(semantic_key_sha256)),
  CHECK (knowledge_kind IN (
    'constraint', 'current_state', 'proposed_feature', 'requirement'
  )),
  CHECK (knowledge_key ~ '^[a-z][a-z0-9_.:-]{1,239}$'),
  CHECK (
    (component_key IS NULL AND binding_source IN (
      'explicit_source_text', 'trusted_thread_binding', 'legacy_root_scope'
    ))
    OR
    (component_key IS NOT NULL
     AND component_key ~ '^[a-z][a-z0-9-]{0,99}$'
     AND component_key NOT LIKE '%--%'
     AND right(component_key, 1) <> '-'
     AND binding_source = 'trusted_component_registry')
  ),
  CHECK (
    (current_revision_id IS NULL AND revision_number = 0)
    OR (current_revision_id IS NOT NULL AND revision_number > 0)
  ),
  CHECK (updated_at >= created_at)
);

-- Additive compatibility for installations that already have the V5 target.
-- Existing project rows predate component provenance and are marked honestly;
-- new inserts have no default and must supply an explicit binding source.
ALTER TABLE memory.project_knowledge_head_v5
  ADD COLUMN IF NOT EXISTS component_key text;
ALTER TABLE memory.project_knowledge_head_v5
  ADD COLUMN IF NOT EXISTS binding_source text NOT NULL
  DEFAULT 'legacy_root_scope';
ALTER TABLE memory.project_knowledge_head_v5
  ALTER COLUMN binding_source DROP DEFAULT;

DO $block$
DECLARE
  constraint_name text;
BEGIN
  SELECT constraint_row.conname INTO constraint_name
  FROM pg_constraint AS constraint_row
  WHERE constraint_row.conrelid = 'memory.project_knowledge_head_v5'::regclass
    AND constraint_row.contype = 'u'
    AND (
      SELECT array_agg(attribute.attname ORDER BY attribute.attname)
      FROM unnest(constraint_row.conkey) AS key(attnum)
      JOIN pg_attribute AS attribute
        ON attribute.attrelid = constraint_row.conrelid
       AND attribute.attnum = key.attnum
    ) = ARRAY['knowledge_key','owner_user_id','project_id']::name[];
  IF constraint_name IS NOT NULL THEN
    EXECUTE format(
      'ALTER TABLE memory.project_knowledge_head_v5 DROP CONSTRAINT %I',
      constraint_name
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.project_knowledge_head_v5'::regclass
      AND conname = 'project_head_v5_scope_check'
  ) THEN
    ALTER TABLE memory.project_knowledge_head_v5
      ADD CONSTRAINT project_head_v5_scope_check CHECK (
        (component_key IS NULL AND binding_source IN (
          'explicit_source_text', 'trusted_thread_binding', 'legacy_root_scope'
        ))
        OR
        (component_key IS NOT NULL
         AND component_key ~ '^[a-z][a-z0-9-]{0,99}$'
         AND component_key NOT LIKE '%--%'
         AND right(component_key, 1) <> '-'
         AND binding_source = 'trusted_component_registry')
      );
  END IF;
  IF to_regclass('memory.project_component_v5') IS NOT NULL
     AND NOT EXISTS (
       SELECT 1 FROM pg_constraint
       WHERE conrelid = 'memory.project_knowledge_head_v5'::regclass
         AND conname = 'project_head_v5_component_fk'
     ) THEN
    ALTER TABLE memory.project_knowledge_head_v5
      ADD CONSTRAINT project_head_v5_component_fk
      FOREIGN KEY (owner_user_id, project_id, component_key)
      REFERENCES memory.project_component_v5(
        owner_user_id, project_id, component_key
      ) ON DELETE RESTRICT;
  END IF;
END
$block$;

CREATE UNIQUE INDEX IF NOT EXISTS project_head_v5_root_knowledge_key_uq
  ON memory.project_knowledge_head_v5(
    owner_user_id, project_id, knowledge_key
  ) WHERE component_key IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS project_head_v5_component_knowledge_key_uq
  ON memory.project_knowledge_head_v5(
    owner_user_id, project_id, component_key, knowledge_key
  ) WHERE component_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory.project_knowledge_revision_v5 (
  revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  knowledge_id uuid NOT NULL,
  revision_number integer NOT NULL,
  canonical_text text NOT NULL,
  content_sha256 text NOT NULL,
  document_state text NOT NULL,
  authority_level text NOT NULL,
  surface_policy memory.observation_surface_policy NOT NULL,
  prior_revision_id uuid,
  effective_source_observation_id uuid,
  effective_precision memory.temporal_precision,
  effective_instant_at timestamptz,
  effective_calendar_range daterange,
  effective_instant_range tstzrange,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, project_id, revision_id),
  UNIQUE (owner_user_id, project_id, knowledge_id, revision_id),
  UNIQUE (owner_user_id, project_id, knowledge_id, revision_number),
  UNIQUE (owner_user_id, project_id, knowledge_id, content_sha256),
  FOREIGN KEY (owner_user_id, project_id, knowledge_id)
    REFERENCES memory.project_knowledge_head_v5(
      owner_user_id, project_id, knowledge_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id, prior_revision_id)
    REFERENCES memory.project_knowledge_revision_v5(
      owner_user_id, project_id, revision_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, effective_source_observation_id)
    REFERENCES memory.observation_temporal(owner_user_id, observation_id)
    ON DELETE RESTRICT,
  CHECK (revision_number > 0),
  CHECK (btrim(canonical_text) <> '' AND length(canonical_text) <= 4000),
  CHECK (memory.v5_sha256_valid(content_sha256)),
  CHECK (document_state IN (
    'unverified', 'working', 'proposed', 'ratified',
    'historical', 'superseded'
  )),
  CHECK (authority_level IN (
    'user_reported', 'user_ratified', 'approved_spec',
    'system_observed', 'external_reference'
  )),
  CHECK (surface_policy = 'exact_project_scope_only'),
  CHECK (prior_revision_id IS NULL OR prior_revision_id <> revision_id),
  CHECK (jsonb_typeof(metadata) = 'object' AND pg_column_size(metadata) <= 16384),
  CHECK (
    (effective_source_observation_id IS NULL
     AND effective_precision IS NULL
     AND effective_instant_at IS NULL
     AND effective_calendar_range IS NULL
     AND effective_instant_range IS NULL)
    OR
    (effective_source_observation_id IS NOT NULL
     AND effective_precision IS NOT NULL
     AND num_nonnulls(
       effective_instant_at,
       effective_calendar_range,
       effective_instant_range
     ) = 1)
  )
);

DO $block$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.project_knowledge_head_v5'::regclass
      AND conname = 'project_head_v5_current_revision_fk'
  ) THEN
    ALTER TABLE memory.project_knowledge_head_v5
      ADD CONSTRAINT project_head_v5_current_revision_fk
      FOREIGN KEY (
        owner_user_id, project_id, knowledge_id, current_revision_id
      ) REFERENCES memory.project_knowledge_revision_v5(
        owner_user_id, project_id, knowledge_id, revision_id
      ) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
  END IF;
END
$block$;

CREATE INDEX IF NOT EXISTS preference_head_v5_owner_status_idx
  ON memory.preference_head_v5(owner_user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS preference_head_v5_owner_current_revision_idx
  ON memory.preference_head_v5(owner_user_id, current_revision_id)
  WHERE current_revision_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS preference_revision_v5_owner_prior_idx
  ON memory.preference_revision_v5(owner_user_id, prior_revision_id)
  WHERE prior_revision_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS project_head_v5_owner_status_idx
  ON memory.project_knowledge_head_v5(
    owner_user_id, project_id, status, updated_at DESC
  );
CREATE INDEX IF NOT EXISTS project_head_v5_owner_current_revision_idx
  ON memory.project_knowledge_head_v5(owner_user_id, current_revision_id)
  WHERE current_revision_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS project_revision_v5_owner_prior_idx
  ON memory.project_knowledge_revision_v5(
    owner_user_id, project_id, prior_revision_id
  ) WHERE prior_revision_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS project_revision_v5_owner_temporal_source_idx
  ON memory.project_knowledge_revision_v5(
    owner_user_id, effective_source_observation_id
  ) WHERE effective_source_observation_id IS NOT NULL;

CREATE OR REPLACE FUNCTION memory.guard_v5_durable_actor()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF current_user <> 'memory_v5_writer' THEN
    RAISE EXCEPTION 'V5 durable target writes require memory_v5_writer'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL OR NEW.owner_user_id <> actor THEN
    RAISE EXCEPTION 'V5 durable target owner does not match current actor'
      USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_preference_head_update_v5()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  revision_matches boolean;
BEGIN
  IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
     OR NEW.preference_id IS DISTINCT FROM OLD.preference_id
     OR NEW.semantic_key_sha256 IS DISTINCT FROM OLD.semantic_key_sha256
     OR NEW.preference_class IS DISTINCT FROM OLD.preference_class
     OR NEW.preference_domain IS DISTINCT FROM OLD.preference_domain
     OR NEW.preference_key IS DISTINCT FROM OLD.preference_key
     OR NEW.scope IS DISTINCT FROM OLD.scope
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'V5 preference head identity is immutable'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.revision_number < OLD.revision_number
     OR NEW.updated_at < OLD.updated_at THEN
    RAISE EXCEPTION 'V5 preference head cannot move backward'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.current_revision_id IS DISTINCT FROM OLD.current_revision_id THEN
    IF NEW.revision_number <> OLD.revision_number + 1 THEN
      RAISE EXCEPTION 'V5 preference head revision must advance exactly once'
        USING ERRCODE = '40001';
    END IF;
    SELECT EXISTS (
      SELECT 1 FROM memory.preference_revision_v5 AS revision
      WHERE revision.owner_user_id = NEW.owner_user_id
        AND revision.preference_id = NEW.preference_id
        AND revision.revision_id = NEW.current_revision_id
        AND revision.revision_number = NEW.revision_number
    ) INTO revision_matches;
    IF NOT revision_matches THEN
      RAISE EXCEPTION 'V5 preference head target revision does not match'
        USING ERRCODE = '23514';
    END IF;
  ELSIF NEW.revision_number <> OLD.revision_number THEN
    RAISE EXCEPTION 'V5 preference revision number changed without a revision'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_preference_revision_insert_v5()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  head memory.preference_head_v5%ROWTYPE;
BEGIN
  SELECT stored.* INTO STRICT head
  FROM memory.preference_head_v5 AS stored
  WHERE stored.owner_user_id = NEW.owner_user_id
    AND stored.preference_id = NEW.preference_id
  FOR UPDATE;
  IF NEW.revision_number <> head.revision_number + 1
     OR NEW.prior_revision_id IS DISTINCT FROM head.current_revision_id THEN
    RAISE EXCEPTION 'V5 preference revision optimistic lock mismatch'
      USING ERRCODE = '40001';
  END IF;
  IF head.preference_class = 'response' THEN
    IF NEW.preference_polarity <> 'not_applicable'
       OR NEW.surface_policy <> 'zero_token_control_only' THEN
      RAISE EXCEPTION 'response preferences are zero-token controls only'
        USING ERRCODE = '23514';
    END IF;
  ELSIF NEW.preference_polarity = 'not_applicable'
        OR NEW.surface_policy NOT IN (
          'relevant_recommendation_or_explicit_recall', 'never'
        ) THEN
    RAISE EXCEPTION 'life preference policy is invalid'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_project_head_update_v5()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  revision_matches boolean;
BEGIN
  IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
     OR NEW.project_id IS DISTINCT FROM OLD.project_id
     OR NEW.component_key IS DISTINCT FROM OLD.component_key
     OR NEW.binding_source IS DISTINCT FROM OLD.binding_source
     OR NEW.knowledge_id IS DISTINCT FROM OLD.knowledge_id
     OR NEW.semantic_key_sha256 IS DISTINCT FROM OLD.semantic_key_sha256
     OR NEW.knowledge_kind IS DISTINCT FROM OLD.knowledge_kind
     OR NEW.knowledge_key IS DISTINCT FROM OLD.knowledge_key
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'V5 project head identity is immutable'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.revision_number < OLD.revision_number
     OR NEW.updated_at < OLD.updated_at THEN
    RAISE EXCEPTION 'V5 project head cannot move backward'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.current_revision_id IS DISTINCT FROM OLD.current_revision_id THEN
    IF NEW.revision_number <> OLD.revision_number + 1 THEN
      RAISE EXCEPTION 'V5 project head revision must advance exactly once'
        USING ERRCODE = '40001';
    END IF;
    SELECT EXISTS (
      SELECT 1 FROM memory.project_knowledge_revision_v5 AS revision
      WHERE revision.owner_user_id = NEW.owner_user_id
        AND revision.project_id = NEW.project_id
        AND revision.knowledge_id = NEW.knowledge_id
        AND revision.revision_id = NEW.current_revision_id
        AND revision.revision_number = NEW.revision_number
    ) INTO revision_matches;
    IF NOT revision_matches THEN
      RAISE EXCEPTION 'V5 project head target revision does not match'
        USING ERRCODE = '23514';
    END IF;
  ELSIF NEW.revision_number <> OLD.revision_number THEN
    RAISE EXCEPTION 'V5 project revision number changed without a revision'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_project_revision_temporal_v5()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  temporal memory.observation_temporal%ROWTYPE;
  head memory.project_knowledge_head_v5%ROWTYPE;
BEGIN
  SELECT stored.* INTO STRICT head
  FROM memory.project_knowledge_head_v5 AS stored
  WHERE stored.owner_user_id = NEW.owner_user_id
    AND stored.project_id = NEW.project_id
    AND stored.knowledge_id = NEW.knowledge_id
  FOR UPDATE;
  IF NEW.revision_number <> head.revision_number + 1
     OR NEW.prior_revision_id IS DISTINCT FROM head.current_revision_id THEN
    RAISE EXCEPTION 'V5 project revision optimistic lock mismatch'
      USING ERRCODE = '40001';
  END IF;
  IF NEW.effective_source_observation_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT stored.* INTO STRICT temporal
  FROM memory.observation_temporal AS stored
  WHERE stored.owner_user_id = NEW.owner_user_id
    AND stored.observation_id = NEW.effective_source_observation_id;
  IF temporal.semantic <> 'state_validity'
     OR temporal.precision <> NEW.effective_precision THEN
    RAISE EXCEPTION
      'project effective interval requires exact state-validity provenance'
      USING ERRCODE = '23514';
  END IF;
  IF temporal.basis = 'instant' THEN
    IF NEW.effective_instant_at IS DISTINCT FROM temporal.instant_at
       OR NEW.effective_instant_range IS DISTINCT FROM temporal.instant_range
       OR NEW.effective_calendar_range IS NOT NULL THEN
      RAISE EXCEPTION 'project instant materialization differs from source observation'
        USING ERRCODE = '23514';
    END IF;
  ELSIF temporal.basis = 'calendar' THEN
    IF NEW.effective_calendar_range IS DISTINCT FROM temporal.calendar_range
       OR NEW.effective_instant_at IS NOT NULL
       OR NEW.effective_instant_range IS NOT NULL THEN
      RAISE EXCEPTION 'project calendar materialization differs from source observation'
        USING ERRCODE = '23514';
    END IF;
  ELSE
    RAISE EXCEPTION 'relative, recurring, and absent time cannot be materialized'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

DO $rls$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'preference_head_v5',
    'preference_revision_v5',
    'project_knowledge_head_v5',
    'project_knowledge_revision_v5'
  ]
  LOOP
    EXECUTE format('ALTER TABLE memory.%I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('ALTER TABLE memory.%I FORCE ROW LEVEL SECURITY', table_name);
    EXECUTE format('DROP POLICY IF EXISTS owner_isolation ON memory.%I', table_name);
    EXECUTE format(
      'CREATE POLICY owner_isolation ON memory.%I TO memory_v5_writer '
      'USING (owner_user_id = (SELECT memory.current_actor_user_id())) '
      'WITH CHECK (owner_user_id = (SELECT memory.current_actor_user_id()))',
      table_name
    );
  END LOOP;
END
$rls$;

DROP TRIGGER IF EXISTS a_preference_head_v5_actor
  ON memory.preference_head_v5;
CREATE TRIGGER a_preference_head_v5_actor
BEFORE INSERT OR UPDATE ON memory.preference_head_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_durable_actor();
DROP TRIGGER IF EXISTS b_preference_head_v5_update
  ON memory.preference_head_v5;
CREATE TRIGGER b_preference_head_v5_update
BEFORE UPDATE ON memory.preference_head_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_preference_head_update_v5();

DROP TRIGGER IF EXISTS a_preference_revision_v5_actor
  ON memory.preference_revision_v5;
CREATE TRIGGER a_preference_revision_v5_actor
BEFORE INSERT ON memory.preference_revision_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_durable_actor();
DROP TRIGGER IF EXISTS b_preference_revision_v5_contract
  ON memory.preference_revision_v5;
CREATE TRIGGER b_preference_revision_v5_contract
BEFORE INSERT ON memory.preference_revision_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_preference_revision_insert_v5();
DROP TRIGGER IF EXISTS c_preference_revision_v5_append_only
  ON memory.preference_revision_v5;
CREATE TRIGGER c_preference_revision_v5_append_only
BEFORE UPDATE OR DELETE ON memory.preference_revision_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

DROP TRIGGER IF EXISTS a_project_head_v5_actor
  ON memory.project_knowledge_head_v5;
CREATE TRIGGER a_project_head_v5_actor
BEFORE INSERT OR UPDATE ON memory.project_knowledge_head_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_durable_actor();
DROP TRIGGER IF EXISTS b_project_head_v5_update
  ON memory.project_knowledge_head_v5;
CREATE TRIGGER b_project_head_v5_update
BEFORE UPDATE ON memory.project_knowledge_head_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_project_head_update_v5();

DROP TRIGGER IF EXISTS a_project_revision_v5_actor
  ON memory.project_knowledge_revision_v5;
CREATE TRIGGER a_project_revision_v5_actor
BEFORE INSERT ON memory.project_knowledge_revision_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_durable_actor();
DROP TRIGGER IF EXISTS b_project_revision_v5_temporal
  ON memory.project_knowledge_revision_v5;
CREATE TRIGGER b_project_revision_v5_temporal
BEFORE INSERT ON memory.project_knowledge_revision_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_project_revision_temporal_v5();
DROP TRIGGER IF EXISTS c_project_revision_v5_append_only
  ON memory.project_knowledge_revision_v5;
CREATE TRIGGER c_project_revision_v5_append_only
BEFORE UPDATE OR DELETE ON memory.project_knowledge_revision_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

ALTER FUNCTION memory.guard_v5_durable_actor()
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_preference_head_update_v5()
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_preference_revision_insert_v5()
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_project_head_update_v5()
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_project_revision_temporal_v5()
  OWNER TO memory_v5_writer;

REVOKE ALL ON
  memory.preference_head_v5,
  memory.preference_revision_v5,
  memory.project_knowledge_head_v5,
  memory.project_knowledge_revision_v5
FROM PUBLIC, brains_app;

GRANT SELECT, INSERT ON
  memory.preference_head_v5,
  memory.preference_revision_v5,
  memory.project_knowledge_head_v5,
  memory.project_knowledge_revision_v5
TO memory_v5_writer;

GRANT UPDATE (
  status, current_revision_id, revision_number, updated_at
) ON memory.preference_head_v5 TO memory_v5_writer;
GRANT UPDATE (
  status, current_revision_id, revision_number, updated_at
) ON memory.project_knowledge_head_v5 TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.guard_v5_durable_actor()
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_preference_head_update_v5()
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_preference_revision_insert_v5()
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_project_head_update_v5()
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_project_revision_temporal_v5()
  FROM PUBLIC, brains_app;

GRANT EXECUTE ON FUNCTION memory.guard_v5_durable_actor()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_preference_head_update_v5()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_preference_revision_insert_v5()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_project_head_update_v5()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_project_revision_temporal_v5()
  TO memory_v5_writer;

COMMIT;
