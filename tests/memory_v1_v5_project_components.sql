\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid regprocedure;
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'memory_v5_extraction_maintainer'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'memory_v5_extraction_maintainer role is unsafe';
  END IF;

  FOREACH function_oid IN ARRAY ARRAY[
    'memory.apply_owner_project_component_v5(uuid,uuid,text,text,uuid,text[],jsonb)'::regprocedure,
    'memory.read_owner_project_components_v5(uuid)'::regprocedure
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM pg_proc
      WHERE oid = function_oid
        AND prosecdef
        AND proowner = 'memory_v5_extraction_maintainer'::regrole
        AND proconfig @> ARRAY[
          'search_path=pg_catalog',
          'row_security=on'
        ]::text[]
    ) THEN
      RAISE EXCEPTION 'project component function % is unsafe',function_oid;
    END IF;
    IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
       OR has_function_privilege('public',function_oid,'EXECUTE') THEN
      RAISE EXCEPTION 'project component function % has unsafe ACL',function_oid;
    END IF;
  END LOOP;

  IF has_table_privilege(
       'brains_app','memory.project_component_v5','INSERT'
     )
     OR has_table_privilege(
       'brains_app','memory.project_component_v5','UPDATE'
     )
     OR has_table_privilege(
       'brains_app','memory.project_component_v5','DELETE'
     )
     OR has_table_privilege(
       'brains_app','memory.project_component_alias_v5','INSERT'
     )
     OR has_table_privilege(
       'brains_app','memory.project_component_registration_event_v5','INSERT'
     ) THEN
    RAISE EXCEPTION 'brains_app has direct project component mutation rights';
  END IF;

  IF (
    SELECT count(*)
    FROM pg_class
    WHERE oid IN (
      'memory.project_component_v5'::regclass,
      'memory.project_component_alias_v5'::regclass,
      'memory.project_component_registration_event_v5'::regclass
    )
      AND relrowsecurity
      AND relforcerowsecurity
  ) <> 3 THEN
    RAISE EXCEPTION 'project component tables lack forced RLS';
  END IF;
END
$security$;

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

INSERT INTO memory.project_space(
  project_id,owner_user_id,project_key,display_name,metadata
) VALUES
  (
    'ca000000-0000-4000-8000-000000000001',
    'ca111111-1111-4111-8111-111111111111',
    'verbal-sage','Verbal Sage','{"test":true}'::jsonb
  ),
  (
    'cb000000-0000-4000-8000-000000000001',
    'cb222222-2222-4222-8222-222222222222',
    'other-product','Other Product','{"test":true}'::jsonb
  );

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','ca111111-1111-4111-8111-111111111111',true
);

SELECT *
FROM memory.apply_owner_project_component_v5(
  'ca100000-0000-4000-8000-000000000001',
  'ca000000-0000-4000-8000-000000000001',
  'memory-v1','Memory V1',NULL,
  ARRAY['Memory system','Memory'],
  '{"component_kind":"memory_architecture"}'::jsonb
)
\gset memory_

SELECT
  1 / ((:'memory_component_key' = 'memory-v1')::integer),
  1 / ((:'memory_outcome' = 'created')::integer),
  1 / ((:'memory_apply_outcome' = 'applied')::integer),
  1 / ((:'memory_aliases' = '{memory,memory-system,memory-v1}')::integer);

SELECT 1 / ((apply_outcome = 'replayed')::integer)
FROM memory.apply_owner_project_component_v5(
  'ca100000-0000-4000-8000-000000000001',
  'ca000000-0000-4000-8000-000000000001',
  'memory-v1','Memory V1',NULL,
  ARRAY['Memory system','Memory'],
  '{"component_kind":"memory_architecture"}'::jsonb
);

SELECT *
FROM memory.apply_owner_project_component_v5(
  'ca100000-0000-4000-8000-000000000002',
  'ca000000-0000-4000-8000-000000000001',
  'lifeswitch','LifeSwitch',NULL,
  ARRAY['Life Switch'],
  '{"component_kind":"structured_application"}'::jsonb
)
\gset lifeswitch_

SELECT *
FROM memory.apply_owner_project_component_v5(
  'ca100000-0000-4000-8000-000000000003',
  'ca000000-0000-4000-8000-000000000001',
  'lifeswitch-nutrition','LifeSwitch Nutrition',
  :'lifeswitch_component_id',
  ARRAY['Nutrition','LifeSwitch food tracking'],
  '{"component_kind":"structured_domain"}'::jsonb
)
\gset nutrition_

SELECT
  1 / ((:'nutrition_parent_component_id' = :'lifeswitch_component_id')::integer),
  1 / ((SELECT count(*) = 3
        FROM memory.read_owner_project_components_v5(
          'ca000000-0000-4000-8000-000000000001'
        ))::integer),
  1 / ((SELECT count(*) = 1
        FROM memory.read_owner_project_components_v5(
          'ca000000-0000-4000-8000-000000000001'
        )
        WHERE component_key = 'lifeswitch-nutrition'
          AND parent_component_id = :'lifeswitch_component_id')::integer);

SELECT
  1 / ((outcome = 'existing')::integer),
  1 / ((apply_outcome = 'applied')::integer)
FROM memory.apply_owner_project_component_v5(
  'ca100000-0000-4000-8000-000000000004',
  'ca000000-0000-4000-8000-000000000001',
  'memory-v1','Memory V1',NULL,
  ARRAY['Memory','Memory system'],
  '{"component_kind":"memory_architecture"}'::jsonb
);

SELECT
  1 / ((SELECT count(*) = 3
        FROM memory.project_component_v5)::integer),
  1 / ((SELECT count(*) = 4
        FROM memory.project_component_registration_event_v5)::integer);

SELECT pg_temp.assert_component_call_denied(
  $sql$SELECT * FROM memory.apply_owner_project_component_v5(
    'ca100000-0000-4000-8000-000000000005',
    'cb000000-0000-4000-8000-000000000001',
    'foreign','Foreign',NULL,ARRAY[]::text[],'{}'::jsonb
  )$sql$
);

SELECT pg_temp.assert_component_call_denied(
  $sql$SELECT * FROM memory.apply_owner_project_component_v5(
    'ca100000-0000-4000-8000-000000000006',
    'ca000000-0000-4000-8000-000000000001',
    'collision','Collision',NULL,ARRAY['Memory'],'{}'::jsonb
  )$sql$
);

SELECT pg_temp.assert_component_call_denied(
  $sql$SELECT * FROM memory.apply_owner_project_component_v5(
    'ca100000-0000-4000-8000-000000000007',
    'ca000000-0000-4000-8000-000000000001',
    'root-collision','Root Collision',NULL,
    ARRAY['Verbal Sage'],'{}'::jsonb
  )$sql$
);

SELECT pg_temp.assert_component_call_denied(
  $sql$SELECT * FROM memory.read_owner_project_components_v5(
    'cb000000-0000-4000-8000-000000000001'
  )$sql$
);

SELECT pg_temp.assert_component_call_denied(
  $sql$INSERT INTO memory.project_component_v5(
    owner_user_id,project_id,component_key,display_name
  ) VALUES (
    'ca111111-1111-4111-8111-111111111111',
    'ca000000-0000-4000-8000-000000000001',
    'direct-write','Direct Write'
  )$sql$
);

SELECT pg_temp.assert_component_call_denied(
  $sql$UPDATE memory.project_component_v5
    SET display_name = 'Changed'
    WHERE component_key = 'memory-v1'$sql$
);

SELECT set_config(
  'app.user_id','cb222222-2222-4222-8222-222222222222',true
);
SELECT
  1 / ((SELECT count(*) = 0
        FROM memory.project_component_v5)::integer),
  1 / ((SELECT count(*) = 0
        FROM memory.project_component_alias_v5)::integer),
  1 / ((SELECT count(*) = 0
        FROM memory.project_component_registration_event_v5)::integer);

SELECT pg_temp.assert_component_call_denied(
  $sql$SELECT * FROM memory.read_owner_project_components_v5(
    'ca000000-0000-4000-8000-000000000001'
  )$sql$
);

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 1 / ((count(*) = 0)::integer)
FROM memory.project_component_v5;
SELECT 1 / ((count(*) = 0)::integer)
FROM memory.project_component_alias_v5;
SELECT 1 / ((count(*) = 0)::integer)
FROM memory.project_component_registration_event_v5;

\echo memory_v1_v5_project_components: PASS
