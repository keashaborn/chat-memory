BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5 project component migration requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regrole('memory_v5_extraction_maintainer') IS NULL
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regclass('memory.project_space') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('memory.guard_v5_extraction_append_only()') IS NULL
     OR to_regprocedure('memory.guard_v5_observation_contract()') IS NULL
     OR to_regprocedure('memory.v5_project_scope_valid(jsonb)') IS NULL THEN
    RAISE EXCEPTION 'V5 project component prerequisites are absent';
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.project_component_v5 (
  component_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  component_key text NOT NULL,
  display_name text NOT NULL,
  parent_component_id uuid,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,project_id,component_id),
  UNIQUE (owner_user_id,project_id,component_key),
  FOREIGN KEY (owner_user_id,project_id)
    REFERENCES memory.project_space(owner_user_id,project_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,project_id,parent_component_id)
    REFERENCES memory.project_component_v5(
      owner_user_id,project_id,component_id
    )
    ON DELETE RESTRICT,
  CHECK (
    component_key ~ '^[a-z][a-z0-9-]{0,99}$'
    AND component_key NOT LIKE '%--%'
    AND right(component_key,1) <> '-'
  ),
  CHECK (btrim(display_name) <> '' AND length(display_name) <= 200),
  CHECK (parent_component_id IS NULL OR parent_component_id <> component_id),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS project_component_v5_parent_idx
  ON memory.project_component_v5(
    owner_user_id,project_id,parent_component_id
  )
  WHERE parent_component_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory.project_component_alias_v5 (
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  component_id uuid NOT NULL,
  normalized_alias text NOT NULL,
  display_alias text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id,project_id,normalized_alias),
  UNIQUE (owner_user_id,project_id,component_id,normalized_alias),
  FOREIGN KEY (owner_user_id,project_id,component_id)
    REFERENCES memory.project_component_v5(
      owner_user_id,project_id,component_id
    )
    ON DELETE RESTRICT,
  CHECK (
    normalized_alias ~ '^[a-z][a-z0-9-]{0,199}$'
    AND normalized_alias NOT LIKE '%--%'
    AND right(normalized_alias,1) <> '-'
  ),
  CHECK (btrim(display_alias) <> '' AND length(display_alias) <= 200)
);

CREATE INDEX IF NOT EXISTS project_component_alias_v5_component_idx
  ON memory.project_component_alias_v5(
    owner_user_id,project_id,component_id
  );

CREATE TABLE IF NOT EXISTS memory.project_component_registration_event_v5 (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  request_sha256 text NOT NULL,
  project_id uuid NOT NULL,
  component_id uuid NOT NULL,
  outcome text NOT NULL,
  actor_user_id uuid NOT NULL,
  invoked_by_role text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,event_id),
  UNIQUE (owner_user_id,operation_id),
  FOREIGN KEY (owner_user_id,project_id,component_id)
    REFERENCES memory.project_component_v5(
      owner_user_id,project_id,component_id
    )
    ON DELETE RESTRICT,
  CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (outcome IN ('created','existing')),
  CHECK (actor_user_id = owner_user_id),
  CHECK (btrim(invoked_by_role) <> '' AND length(invoked_by_role) <= 200),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS project_component_registration_event_v5_component_idx
  ON memory.project_component_registration_event_v5(
    owner_user_id,project_id,component_id,created_at DESC
  );

ALTER TABLE memory.project_component_v5 OWNER TO sage;
ALTER TABLE memory.project_component_alias_v5 OWNER TO sage;
ALTER TABLE memory.project_component_registration_event_v5 OWNER TO sage;

DROP TRIGGER IF EXISTS project_component_v5_append_only_guard
  ON memory.project_component_v5;
CREATE TRIGGER project_component_v5_append_only_guard
BEFORE UPDATE OR DELETE ON memory.project_component_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_extraction_append_only();

DROP TRIGGER IF EXISTS project_component_alias_v5_append_only_guard
  ON memory.project_component_alias_v5;
CREATE TRIGGER project_component_alias_v5_append_only_guard
BEFORE UPDATE OR DELETE ON memory.project_component_alias_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_extraction_append_only();

DROP TRIGGER IF EXISTS project_component_registration_event_v5_append_only_guard
  ON memory.project_component_registration_event_v5;
CREATE TRIGGER project_component_registration_event_v5_append_only_guard
BEFORE UPDATE OR DELETE ON memory.project_component_registration_event_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_extraction_append_only();

ALTER TABLE memory.project_component_v5 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.project_component_v5 FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.project_component_v5;
CREATE POLICY owner_isolation ON memory.project_component_v5
  USING (owner_user_id = (SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id = (SELECT memory.current_actor_user_id()));

ALTER TABLE memory.project_component_alias_v5 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.project_component_alias_v5 FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.project_component_alias_v5;
CREATE POLICY owner_isolation ON memory.project_component_alias_v5
  USING (owner_user_id = (SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id = (SELECT memory.current_actor_user_id()));

ALTER TABLE memory.project_component_registration_event_v5
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.project_component_registration_event_v5
  FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.project_component_registration_event_v5;
CREATE POLICY owner_isolation
  ON memory.project_component_registration_event_v5
  USING (owner_user_id = (SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id = (SELECT memory.current_actor_user_id()));

CREATE OR REPLACE FUNCTION memory.normalize_project_component_alias_v5(
  p_value text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
SET search_path = pg_catalog
AS $function$
  SELECT btrim(
    regexp_replace(lower(btrim(p_value)),'[^a-z0-9]+','-','g'),
    '-'
  )
$function$;

CREATE OR REPLACE FUNCTION memory.v5_project_scope_valid(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  key_count integer;
  state text;
  project_key text;
  component_key text;
  binding_source text;
  has_component_key boolean;
BEGIN
  IF jsonb_typeof(value) <> 'object' THEN
    RETURN false;
  END IF;
  SELECT count(*) INTO key_count FROM jsonb_object_keys(value);
  state := value->>'state';
  project_key := value->>'project_key';
  component_key := value->>'component_key';
  binding_source := value->>'binding_source';
  has_component_key := value ? 'component_key';

  IF key_count NOT IN (3,4)
     OR NOT (value ?& ARRAY['state','project_key','binding_source'])
     OR (key_count = 4 AND NOT has_component_key)
     OR state NOT IN ('not_applicable','resolved','unresolved') THEN
    RETURN false;
  END IF;

  IF NOT has_component_key THEN
    RETURN binding_source IN (
      'not_applicable','explicit_source_text',
      'trusted_thread_binding','unresolved'
    ) AND (
      (state = 'not_applicable'
       AND value->'project_key' = 'null'::jsonb
       AND binding_source = 'not_applicable')
      OR
      (state = 'unresolved'
       AND value->'project_key' = 'null'::jsonb
       AND binding_source = 'unresolved')
      OR
      (state = 'resolved'
       AND project_key IS NOT NULL
       AND btrim(project_key) <> ''
       AND length(project_key) <= 500
       AND binding_source IN ('explicit_source_text','trusted_thread_binding'))
    );
  END IF;

  RETURN binding_source IN (
    'not_applicable','explicit_source_text','trusted_component_registry',
    'trusted_thread_binding','unresolved'
  ) AND (
    (state = 'not_applicable'
     AND value->'project_key' = 'null'::jsonb
     AND value->'component_key' = 'null'::jsonb
     AND binding_source = 'not_applicable')
    OR
    (state = 'unresolved'
     AND value->'project_key' = 'null'::jsonb
     AND value->'component_key' = 'null'::jsonb
     AND binding_source = 'unresolved')
    OR
    (state = 'resolved'
     AND project_key IS NOT NULL
     AND btrim(project_key) <> ''
     AND length(project_key) <= 500
     AND (
       (value->'component_key' = 'null'::jsonb
        AND binding_source IN ('explicit_source_text','trusted_thread_binding'))
       OR
       (component_key IS NOT NULL
        AND component_key ~ '^[a-z][a-z0-9-]{0,99}$'
        AND component_key NOT LIKE '%--%'
        AND right(component_key,1) <> '-'
        AND binding_source = 'trusted_component_registry')
     ))
  );
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_v5_observation_contract()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  contract_kind text;
  can_extract boolean;
  subject_mention record;
BEGIN
  SELECT contract.object_kind, contract.extraction_allowed
  INTO contract_kind, can_extract
  FROM memory.predicate_contract AS contract
  WHERE contract.predicate = NEW.predicate
    AND contract.registry_version = NEW.predicate_registry_version;
  IF NOT FOUND OR NOT can_extract THEN
    RAISE EXCEPTION 'predicate is not extraction-enabled in V5: %', NEW.predicate
      USING ERRCODE = '23514';
  END IF;
  IF (contract_kind = 'entity') <> (NEW.object_mention_id IS NOT NULL) THEN
    RAISE EXCEPTION 'predicate object kind mismatch for %', NEW.predicate
      USING ERRCODE = '23514';
  END IF;
  IF NEW.predicate LIKE 'project.%' THEN
    IF NEW.project_scope->>'state' <> 'resolved' THEN
      RAISE EXCEPTION 'project predicate requires trusted resolved project scope'
        USING ERRCODE = '23514';
    END IF;
    IF NEW.projection_class <> 'project_knowledge'
       OR NEW.surface_policy <> 'exact_project_scope_only' THEN
      RAISE EXCEPTION 'project predicate requires project-only projection policy'
        USING ERRCODE = '23514';
    END IF;
    IF NEW.project_scope->>'component_key' IS NOT NULL THEN
      SELECT mention.entity_type, mention.mention_kind, mention.name_text
      INTO subject_mention
      FROM memory.entity_mention AS mention
      WHERE mention.owner_user_id = NEW.owner_user_id
        AND mention.evidence_id = NEW.evidence_id
        AND mention.mention_id = NEW.subject_mention_id;
      IF NOT FOUND
         OR subject_mention.entity_type <> 'project'
         OR subject_mention.mention_kind <> 'named'
         OR coalesce(btrim(subject_mention.name_text),'') = ''
         OR NEW.project_scope->>'binding_source'
              <> 'trusted_component_registry'
         OR NOT EXISTS (
           SELECT 1
           FROM memory.project_space AS project
           JOIN memory.project_component_v5 AS component
             ON component.owner_user_id = project.owner_user_id
            AND component.project_id = project.project_id
           JOIN memory.project_component_alias_v5 AS alias
             ON alias.owner_user_id = component.owner_user_id
            AND alias.project_id = component.project_id
            AND alias.component_id = component.component_id
           WHERE project.owner_user_id = NEW.owner_user_id
             AND project.project_key = NEW.project_scope->>'project_key'
             AND component.component_key
                   = NEW.project_scope->>'component_key'
             AND alias.normalized_alias
                   = memory.normalize_project_component_alias_v5(
                     subject_mention.name_text
                   )
         ) THEN
        RAISE EXCEPTION
          'component project scope is absent from the owner registry'
          USING ERRCODE = '23514';
      END IF;
    END IF;
  ELSIF NEW.project_scope->>'state' <> 'not_applicable' THEN
    RAISE EXCEPTION 'non-project predicate cannot carry project scope'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.apply_owner_project_component_v5(
  p_operation_id uuid,
  p_project_id uuid,
  p_component_key text,
  p_display_name text,
  p_parent_component_id uuid,
  p_aliases text[],
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(
  event_id uuid,
  project_id uuid,
  component_id uuid,
  component_key text,
  display_name text,
  parent_component_id uuid,
  aliases text[],
  outcome text,
  apply_outcome text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $function$
DECLARE
  actor uuid;
  canonical_key text;
  canonical_display text;
  canonical_aliases text[];
  request_sha text;
  existing_component memory.project_component_v5%ROWTYPE;
  project_record memory.project_space%ROWTYPE;
  existing_event memory.project_component_registration_event_v5%ROWTYPE;
  new_component_id uuid;
  new_event memory.project_component_registration_event_v5%ROWTYPE;
  existing_aliases text[];
BEGIN
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_operation_id IS NULL OR p_project_id IS NULL THEN
    RAISE EXCEPTION 'operation_id and project_id are required'
      USING ERRCODE = '22023';
  END IF;

  canonical_key := memory.normalize_project_component_alias_v5(
    p_component_key
  );
  canonical_display := btrim(p_display_name);
  IF canonical_key IS NULL
     OR canonical_key = ''
     OR length(canonical_key) > 100
     OR canonical_key !~ '^[a-z][a-z0-9-]*$'
     OR canonical_key LIKE '%--%'
     OR right(canonical_key,1) = '-' THEN
    RAISE EXCEPTION 'component_key is invalid'
      USING ERRCODE = '22023';
  END IF;
  IF canonical_display IS NULL
     OR canonical_display = ''
     OR length(canonical_display) > 200 THEN
    RAISE EXCEPTION 'display_name must contain 1 to 200 characters'
      USING ERRCODE = '22023';
  END IF;
  IF p_aliases IS NULL OR cardinality(p_aliases) > 20 THEN
    RAISE EXCEPTION 'aliases must be a non-null array of at most 20 values'
      USING ERRCODE = '22023';
  END IF;
  IF p_metadata IS NULL
     OR jsonb_typeof(p_metadata) <> 'object'
     OR pg_column_size(p_metadata) > 16384 THEN
    RAISE EXCEPTION 'metadata must be an object no larger than 16 KiB'
      USING ERRCODE = '22023';
  END IF;

  SELECT coalesce(array_agg(alias_value ORDER BY alias_value),ARRAY[]::text[])
  INTO canonical_aliases
  FROM (
    SELECT DISTINCT memory.normalize_project_component_alias_v5(raw_value)
      AS alias_value
    FROM unnest(
      p_aliases || ARRAY[p_component_key,p_display_name]
    ) AS requested(raw_value)
    WHERE raw_value IS NOT NULL
      AND btrim(raw_value) <> ''
  ) AS normalized
  WHERE alias_value ~ '^[a-z][a-z0-9-]{0,199}$'
    AND alias_value NOT LIKE '%--%'
    AND right(alias_value,1) <> '-';

  IF cardinality(canonical_aliases) < 1
     OR EXISTS (
       SELECT 1
       FROM unnest(p_aliases) AS requested(raw_value)
       WHERE raw_value IS NULL
          OR btrim(raw_value) = ''
          OR length(raw_value) > 200
          OR memory.normalize_project_component_alias_v5(raw_value) = ''
     ) THEN
    RAISE EXCEPTION 'aliases contain an invalid value'
      USING ERRCODE = '22023';
  END IF;

  request_sha := encode(public.digest(convert_to(
    jsonb_build_object(
      'operation','register_project_component_v5',
      'project_id',p_project_id,
      'component_key',canonical_key,
      'display_name',canonical_display,
      'parent_component_id',p_parent_component_id,
      'aliases',to_jsonb(canonical_aliases),
      'metadata',p_metadata
    )::text,
    'UTF8'
  ),'sha256'),'hex');

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':project_component_operation:' || p_operation_id::text,
    0
  ));

  SELECT stored.*
  INTO existing_event
  FROM memory.project_component_registration_event_v5 AS stored
  WHERE stored.owner_user_id = actor
    AND stored.operation_id = p_operation_id;
  IF FOUND THEN
    IF existing_event.request_sha256 <> request_sha THEN
      RAISE EXCEPTION 'operation_id was reused with different component inputs'
        USING ERRCODE = '22023';
    END IF;
    SELECT component.*
    INTO existing_component
    FROM memory.project_component_v5 AS component
    WHERE component.owner_user_id = actor
      AND component.project_id = existing_event.project_id
      AND component.component_id = existing_event.component_id;
    SELECT array_agg(alias.normalized_alias ORDER BY alias.normalized_alias)
    INTO existing_aliases
    FROM memory.project_component_alias_v5 AS alias
    WHERE alias.owner_user_id = actor
      AND alias.project_id = existing_component.project_id
      AND alias.component_id = existing_component.component_id;
    RETURN QUERY SELECT
      existing_event.event_id,
      existing_component.project_id,
      existing_component.component_id,
      existing_component.component_key,
      existing_component.display_name,
      existing_component.parent_component_id,
      existing_aliases,
      existing_event.outcome,
      'replayed'::text;
    RETURN;
  END IF;

  SELECT project.*
  INTO project_record
  FROM memory.project_space AS project
  WHERE project.owner_user_id = actor
    AND project.project_id = p_project_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner project is not visible'
      USING ERRCODE = 'P0002';
  END IF;
  IF canonical_aliases && ARRAY[
    memory.normalize_project_component_alias_v5(project_record.project_key),
    memory.normalize_project_component_alias_v5(project_record.display_name)
  ] THEN
    RAISE EXCEPTION 'component alias conflicts with the project root'
      USING ERRCODE = '23514';
  END IF;

  IF p_parent_component_id IS NOT NULL THEN
    PERFORM 1
    FROM memory.project_component_v5 AS parent
    WHERE parent.owner_user_id = actor
      AND parent.project_id = p_project_id
      AND parent.component_id = p_parent_component_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'parent component is not visible in this project'
        USING ERRCODE = 'P0002';
    END IF;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':project_component:' || p_project_id::text || ':' || canonical_key,
    0
  ));

  SELECT component.*
  INTO existing_component
  FROM memory.project_component_v5 AS component
  WHERE component.owner_user_id = actor
    AND component.project_id = p_project_id
    AND component.component_key = canonical_key;
  IF FOUND THEN
    SELECT array_agg(alias.normalized_alias ORDER BY alias.normalized_alias)
    INTO existing_aliases
    FROM memory.project_component_alias_v5 AS alias
    WHERE alias.owner_user_id = actor
      AND alias.project_id = p_project_id
      AND alias.component_id = existing_component.component_id;
    IF existing_component.display_name <> canonical_display
       OR existing_component.parent_component_id
          IS DISTINCT FROM p_parent_component_id
       OR existing_component.metadata <> p_metadata
       OR existing_aliases IS DISTINCT FROM canonical_aliases THEN
      RAISE EXCEPTION 'component key already exists with different immutable inputs'
        USING ERRCODE = '23514';
    END IF;
    new_component_id := existing_component.component_id;
  ELSE
    new_component_id := gen_random_uuid();
    INSERT INTO memory.project_component_v5(
      component_id,owner_user_id,project_id,component_key,display_name,
      parent_component_id,metadata
    ) VALUES (
      new_component_id,actor,p_project_id,canonical_key,canonical_display,
      p_parent_component_id,p_metadata
    );
    INSERT INTO memory.project_component_alias_v5(
      owner_user_id,project_id,component_id,normalized_alias,display_alias
    )
    SELECT actor,p_project_id,new_component_id,alias_value,alias_value
    FROM unnest(canonical_aliases) AS requested(alias_value)
    ORDER BY alias_value;
  END IF;

  INSERT INTO memory.project_component_registration_event_v5(
    owner_user_id,operation_id,request_sha256,project_id,component_id,
    outcome,actor_user_id,invoked_by_role,metadata
  ) VALUES (
    actor,p_operation_id,request_sha,p_project_id,new_component_id,
    CASE WHEN existing_component.component_id IS NULL
      THEN 'created' ELSE 'existing' END,
    actor,session_user,p_metadata
  )
  RETURNING * INTO new_event;

  RETURN QUERY SELECT
    new_event.event_id,
    p_project_id,
    new_component_id,
    canonical_key,
    canonical_display,
    p_parent_component_id,
    canonical_aliases,
    new_event.outcome,
    'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION memory.read_owner_project_components_v5(
  p_project_id uuid
)
RETURNS TABLE(
  component_id uuid,
  component_key text,
  display_name text,
  parent_component_id uuid,
  aliases text[]
)
LANGUAGE plpgsql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog
SET row_security = on
AS $function$
DECLARE
  actor uuid;
  component_count integer;
BEGIN
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  PERFORM 1
  FROM memory.project_space AS project
  WHERE project.owner_user_id = actor
    AND project.project_id = p_project_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner project is not visible'
      USING ERRCODE = 'P0002';
  END IF;
  SELECT count(*)::integer
  INTO component_count
  FROM memory.project_component_v5 AS component
  WHERE component.owner_user_id = actor
    AND component.project_id = p_project_id;
  IF component_count > 100 THEN
    RAISE EXCEPTION 'project component registry exceeds the bounded read limit'
      USING ERRCODE = '54000';
  END IF;
  RETURN QUERY
  SELECT
    component.component_id,
    component.component_key,
    component.display_name,
    component.parent_component_id,
    array_agg(alias.normalized_alias ORDER BY alias.normalized_alias)
  FROM memory.project_component_v5 AS component
  JOIN memory.project_component_alias_v5 AS alias
    ON alias.owner_user_id = component.owner_user_id
   AND alias.project_id = component.project_id
   AND alias.component_id = component.component_id
  WHERE component.owner_user_id = actor
    AND component.project_id = p_project_id
  GROUP BY
    component.component_id,
    component.component_key,
    component.display_name,
    component.parent_component_id
  ORDER BY component.component_key;
END
$function$;

REVOKE ALL ON
  memory.project_component_v5,
  memory.project_component_alias_v5,
  memory.project_component_registration_event_v5
FROM PUBLIC,brains_app;

GRANT SELECT ON
  memory.project_component_v5,
  memory.project_component_alias_v5,
  memory.project_component_registration_event_v5
TO brains_app;

GRANT USAGE ON SCHEMA memory TO memory_v5_extraction_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_extraction_maintainer;
GRANT EXECUTE ON FUNCTION public.digest(bytea,text)
  TO memory_v5_extraction_maintainer;
GRANT SELECT ON
  memory.project_space,
  memory.project_component_v5,
  memory.project_component_alias_v5,
  memory.project_component_registration_event_v5
TO memory_v5_extraction_maintainer;
GRANT SELECT ON
  memory.project_space,
  memory.project_component_v5,
  memory.project_component_alias_v5
TO memory_v5_writer;
GRANT INSERT ON
  memory.project_component_v5,
  memory.project_component_alias_v5,
  memory.project_component_registration_event_v5
TO memory_v5_extraction_maintainer;

ALTER FUNCTION memory.apply_owner_project_component_v5(
  uuid,uuid,text,text,uuid,text[],jsonb
) OWNER TO memory_v5_extraction_maintainer;
ALTER FUNCTION memory.read_owner_project_components_v5(uuid)
  OWNER TO memory_v5_extraction_maintainer;
ALTER FUNCTION memory.normalize_project_component_alias_v5(text)
  OWNER TO memory_v5_extraction_maintainer;

REVOKE ALL ON FUNCTION memory.normalize_project_component_alias_v5(text)
  FROM PUBLIC,brains_app,memory_v5_extraction_maintainer;
REVOKE ALL ON FUNCTION memory.apply_owner_project_component_v5(
  uuid,uuid,text,text,uuid,text[],jsonb
) FROM PUBLIC,brains_app,memory_v5_extraction_maintainer;
REVOKE ALL ON FUNCTION memory.read_owner_project_components_v5(uuid)
  FROM PUBLIC,brains_app,memory_v5_extraction_maintainer;

GRANT EXECUTE ON FUNCTION memory.apply_owner_project_component_v5(
  uuid,uuid,text,text,uuid,text[],jsonb
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.read_owner_project_components_v5(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.normalize_project_component_alias_v5(text)
  TO memory_v5_extraction_maintainer;
GRANT EXECUTE ON FUNCTION memory.normalize_project_component_alias_v5(text)
  TO memory_v5_writer;

COMMIT;
