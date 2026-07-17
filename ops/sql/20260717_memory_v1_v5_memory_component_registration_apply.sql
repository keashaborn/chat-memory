\set ON_ERROR_STOP on

DO $preflight$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'Memory V1 component registration requires sage';
  END IF;
  IF (SELECT count(*) FROM memory.project_component_v5) <> 0
     OR (SELECT count(*) FROM memory.project_component_alias_v5) <> 0
     OR (SELECT count(*) FROM memory.project_component_registration_event_v5) <> 0 THEN
    RAISE EXCEPTION 'component registry is not empty';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM memory.project_space
    WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
      AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
      AND project_key='verbal-sage'
      AND display_name='Verbal Sage'
  ) THEN
    RAISE EXCEPTION 'authorized owner project is absent or changed';
  END IF;
  IF NOT has_function_privilege(
    'brains_app',
    'memory.apply_owner_project_component_v5(uuid,uuid,text,text,uuid,text[],jsonb)',
    'EXECUTE'
  ) OR NOT has_function_privilege(
    'brains_app','memory.read_owner_project_components_v5(uuid)','EXECUTE'
  ) THEN
    RAISE EXCEPTION 'component API ACL is absent';
  END IF;
END
$preflight$;

BEGIN;

CREATE FUNCTION pg_temp.assert_component_call_denied(p_sql text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
  state text;
BEGIN
  BEGIN
    EXECUTE p_sql;
  EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS state = RETURNED_SQLSTATE;
    denied := state IN ('22023','23503','23505','23514','42501','P0002');
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'unsafe project component call was accepted: %',p_sql;
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

SELECT
  1 / ((component_key = 'memory-v1')::integer),
  1 / ((display_name = 'Memory V1')::integer),
  1 / ((parent_component_id IS NULL)::integer),
  1 / ((aliases = ARRAY['memory-v1'])::integer),
  1 / ((outcome = 'created')::integer),
  1 / ((apply_outcome = 'applied')::integer)
FROM memory.apply_owner_project_component_v5(
  '9698390e-cfd7-4db6-9af0-df7afb016ffb',
  '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
  'memory-v1','Memory V1',NULL,ARRAY[]::text[],
  '{"component_kind":"memory_architecture","registration_source":"controlled_registry_v1"}'::jsonb
);

SELECT 1 / ((apply_outcome = 'replayed')::integer)
FROM memory.apply_owner_project_component_v5(
  '9698390e-cfd7-4db6-9af0-df7afb016ffb',
  '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
  'memory-v1','Memory V1',NULL,ARRAY[]::text[],
  '{"component_kind":"memory_architecture","registration_source":"controlled_registry_v1"}'::jsonb
);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT pg_temp.assert_component_call_denied(
  $sql$SELECT * FROM memory.read_owner_project_components_v5(
    '08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
  )$sql$
);

RESET SESSION AUTHORIZATION;

SELECT
  1 / ((SELECT count(*) FROM memory.project_component_v5
         WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
           AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
           AND component_key='memory-v1') = 1)::integer,
  1 / ((SELECT count(*) FROM memory.project_component_alias_v5
         WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
           AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
           AND normalized_alias='memory-v1') = 1)::integer,
  1 / ((SELECT count(*) FROM memory.project_component_registration_event_v5
         WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
           AND operation_id='9698390e-cfd7-4db6-9af0-df7afb016ffb'
           AND outcome='created') = 1)::integer,
  1 / ((SELECT count(*) FROM memory.project_component_v5
         WHERE owner_user_id<>'1240822d-ac9a-4096-95aa-e2b24d36ef50') = 0)::integer;

COMMIT;

-- Prove persisted replay is still zero-write in a new transaction.
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT 1 / ((apply_outcome = 'replayed')::integer)
FROM memory.apply_owner_project_component_v5(
  '9698390e-cfd7-4db6-9af0-df7afb016ffb',
  '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
  'memory-v1','Memory V1',NULL,ARRAY[]::text[],
  '{"component_kind":"memory_architecture","registration_source":"controlled_registry_v1"}'::jsonb
);
RESET SESSION AUTHORIZATION;
COMMIT;

\echo memory_v1_v5_memory_component_registration_apply: PASS
