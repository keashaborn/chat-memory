BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5 project component alias migration requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regrole('memory_v5_extraction_maintainer') IS NULL
     OR to_regclass('memory.project_space') IS NULL
     OR to_regclass('memory.project_component_v5') IS NULL
     OR to_regclass('memory.project_component_alias_v5') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('memory.guard_v5_extraction_append_only()') IS NULL
     OR to_regprocedure('memory.normalize_project_component_alias_v5(text)') IS NULL THEN
    RAISE EXCEPTION 'V5 project component alias prerequisites are absent';
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.project_component_alias_registration_event_v5 (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  request_sha256 text NOT NULL,
  project_id uuid NOT NULL,
  component_id uuid NOT NULL,
  normalized_alias text NOT NULL,
  display_alias text NOT NULL,
  outcome text NOT NULL,
  actor_user_id uuid NOT NULL,
  invoked_by_role text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,event_id),
  UNIQUE (owner_user_id,operation_id),
  FOREIGN KEY (
    owner_user_id,project_id,component_id,normalized_alias
  ) REFERENCES memory.project_component_alias_v5(
    owner_user_id,project_id,component_id,normalized_alias
  ) ON DELETE RESTRICT,
  CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (
    normalized_alias ~ '^[a-z][a-z0-9-]{0,199}$'
    AND normalized_alias NOT LIKE '%--%'
    AND right(normalized_alias,1) <> '-'
  ),
  CHECK (btrim(display_alias) <> '' AND length(display_alias) <= 200),
  CHECK (outcome IN ('added','existing')),
  CHECK (actor_user_id = owner_user_id),
  CHECK (btrim(invoked_by_role) <> '' AND length(invoked_by_role) <= 200),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS project_component_alias_registration_event_v5_component_idx
  ON memory.project_component_alias_registration_event_v5(
    owner_user_id,project_id,component_id,created_at DESC
  );

ALTER TABLE memory.project_component_alias_registration_event_v5 OWNER TO sage;

DROP TRIGGER IF EXISTS project_component_alias_registration_event_v5_append_only_guard
  ON memory.project_component_alias_registration_event_v5;
CREATE TRIGGER project_component_alias_registration_event_v5_append_only_guard
BEFORE UPDATE OR DELETE ON memory.project_component_alias_registration_event_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_extraction_append_only();

ALTER TABLE memory.project_component_alias_registration_event_v5
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.project_component_alias_registration_event_v5
  FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.project_component_alias_registration_event_v5;
CREATE POLICY owner_isolation
  ON memory.project_component_alias_registration_event_v5
  USING (owner_user_id = (SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id = (SELECT memory.current_actor_user_id()));

CREATE OR REPLACE FUNCTION memory.apply_owner_project_component_alias_v5(
  p_operation_id uuid,
  p_project_id uuid,
  p_component_id uuid,
  p_display_alias text,
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(
  event_id uuid,
  project_id uuid,
  component_id uuid,
  normalized_alias text,
  display_alias text,
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
  canonical_display text;
  canonical_alias text;
  request_sha text;
  project_record memory.project_space%ROWTYPE;
  component_record memory.project_component_v5%ROWTYPE;
  alias_record memory.project_component_alias_v5%ROWTYPE;
  existing_event memory.project_component_alias_registration_event_v5%ROWTYPE;
  new_event memory.project_component_alias_registration_event_v5%ROWTYPE;
  result_outcome text;
BEGIN
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_project_id IS NULL
     OR p_component_id IS NULL THEN
    RAISE EXCEPTION 'operation_id, project_id, and component_id are required'
      USING ERRCODE = '22023';
  END IF;

  canonical_display := btrim(p_display_alias);
  canonical_alias := memory.normalize_project_component_alias_v5(
    canonical_display
  );
  IF canonical_display IS NULL
     OR canonical_display = ''
     OR length(canonical_display) > 200
     OR canonical_alias IS NULL
     OR canonical_alias = ''
     OR canonical_alias !~ '^[a-z][a-z0-9-]{0,199}$'
     OR canonical_alias LIKE '%--%'
     OR right(canonical_alias,1) = '-' THEN
    RAISE EXCEPTION 'display_alias is invalid'
      USING ERRCODE = '22023';
  END IF;
  IF p_metadata IS NULL
     OR jsonb_typeof(p_metadata) <> 'object'
     OR pg_column_size(p_metadata) > 16384 THEN
    RAISE EXCEPTION 'metadata must be an object no larger than 16 KiB'
      USING ERRCODE = '22023';
  END IF;

  request_sha := encode(public.digest(convert_to(
    jsonb_build_object(
      'operation','register_project_component_alias_v5',
      'project_id',p_project_id,
      'component_id',p_component_id,
      'normalized_alias',canonical_alias,
      'display_alias',canonical_display,
      'metadata',p_metadata
    )::text,
    'UTF8'
  ),'sha256'),'hex');

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':project_component_alias_operation:'
      || p_operation_id::text,
    0
  ));

  SELECT stored.*
  INTO existing_event
  FROM memory.project_component_alias_registration_event_v5 AS stored
  WHERE stored.owner_user_id = actor
    AND stored.operation_id = p_operation_id;
  IF FOUND THEN
    IF existing_event.request_sha256 <> request_sha THEN
      RAISE EXCEPTION 'operation_id was reused with different alias inputs'
        USING ERRCODE = '22023';
    END IF;
    RETURN QUERY SELECT
      existing_event.event_id,
      existing_event.project_id,
      existing_event.component_id,
      existing_event.normalized_alias,
      existing_event.display_alias,
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

  SELECT component.*
  INTO component_record
  FROM memory.project_component_v5 AS component
  WHERE component.owner_user_id = actor
    AND component.project_id = p_project_id
    AND component.component_id = p_component_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner project component is not visible'
      USING ERRCODE = 'P0002';
  END IF;

  IF canonical_alias IN (
    memory.normalize_project_component_alias_v5(project_record.project_key),
    memory.normalize_project_component_alias_v5(project_record.display_name)
  ) THEN
    RAISE EXCEPTION 'component alias conflicts with the project root'
      USING ERRCODE = '23514';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || ':project_component_alias:'
      || p_project_id::text || ':' || canonical_alias,
    0
  ));

  SELECT alias.*
  INTO alias_record
  FROM memory.project_component_alias_v5 AS alias
  WHERE alias.owner_user_id = actor
    AND alias.project_id = p_project_id
    AND alias.normalized_alias = canonical_alias;
  IF FOUND THEN
    IF alias_record.component_id <> p_component_id THEN
      RAISE EXCEPTION 'component alias is already bound to another component'
        USING ERRCODE = '23514';
    END IF;
    result_outcome := 'existing';
    canonical_display := alias_record.display_alias;
  ELSE
    INSERT INTO memory.project_component_alias_v5(
      owner_user_id,project_id,component_id,normalized_alias,display_alias
    ) VALUES (
      actor,p_project_id,p_component_id,canonical_alias,canonical_display
    )
    RETURNING * INTO alias_record;
    result_outcome := 'added';
  END IF;

  INSERT INTO memory.project_component_alias_registration_event_v5(
    owner_user_id,operation_id,request_sha256,project_id,component_id,
    normalized_alias,display_alias,outcome,actor_user_id,invoked_by_role,
    metadata
  ) VALUES (
    actor,p_operation_id,request_sha,p_project_id,p_component_id,
    canonical_alias,canonical_display,result_outcome,actor,session_user,
    p_metadata
  )
  RETURNING * INTO new_event;

  RETURN QUERY SELECT
    new_event.event_id,
    new_event.project_id,
    new_event.component_id,
    new_event.normalized_alias,
    new_event.display_alias,
    new_event.outcome,
    'applied'::text;
END
$function$;

REVOKE ALL ON memory.project_component_alias_registration_event_v5
FROM PUBLIC,brains_app;

GRANT SELECT,INSERT ON memory.project_component_alias_registration_event_v5
TO memory_v5_extraction_maintainer;

ALTER FUNCTION memory.apply_owner_project_component_alias_v5(
  uuid,uuid,uuid,text,jsonb
) OWNER TO memory_v5_extraction_maintainer;

REVOKE ALL ON FUNCTION memory.apply_owner_project_component_alias_v5(
  uuid,uuid,uuid,text,jsonb
) FROM PUBLIC,brains_app,memory_v5_extraction_maintainer;

GRANT EXECUTE ON FUNCTION memory.apply_owner_project_component_alias_v5(
  uuid,uuid,uuid,text,jsonb
) TO brains_app;

COMMIT;
