\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid regprocedure :=
    'memory.apply_owner_project_component_alias_v5(uuid,uuid,uuid,text,jsonb)'::regprocedure;
BEGIN
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
    RAISE EXCEPTION 'project component alias function is unsafe';
  END IF;
  IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
     OR has_function_privilege('public',function_oid,'EXECUTE') THEN
    RAISE EXCEPTION 'project component alias function has unsafe ACL';
  END IF;
  IF has_table_privilege(
       'brains_app',
       'memory.project_component_alias_registration_event_v5',
       'INSERT'
     )
     OR has_table_privilege(
       'brains_app','memory.project_component_alias_v5','INSERT'
     ) THEN
    RAISE EXCEPTION 'brains_app has direct alias mutation rights';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_class
    WHERE oid =
      'memory.project_component_alias_registration_event_v5'::regclass
      AND relrowsecurity
      AND relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'alias registration event table lacks forced RLS';
  END IF;
END
$security$;

BEGIN;

CREATE FUNCTION pg_temp.assert_alias_call_denied(p_sql text)
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
    RAISE EXCEPTION 'unsafe project component alias call was accepted: %',p_sql;
  END IF;
END
$function$;

INSERT INTO memory.project_space(
  project_id,owner_user_id,project_key,display_name,metadata
) VALUES
  (
    'da000000-0000-4000-8000-000000000001',
    'da111111-1111-4111-8111-111111111111',
    'verbal-sage','Verbal Sage','{"test":true}'::jsonb
  ),
  (
    'db000000-0000-4000-8000-000000000001',
    'db222222-2222-4222-8222-222222222222',
    'other-product','Other Product','{"test":true}'::jsonb
  );

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','da111111-1111-4111-8111-111111111111',true
);

SELECT *
FROM memory.apply_owner_project_component_v5(
  'da100000-0000-4000-8000-000000000001',
  'da000000-0000-4000-8000-000000000001',
  'memory-v1','Memory V1',NULL,ARRAY[]::text[],
  '{"component_kind":"memory_architecture"}'::jsonb
)
\gset memory_

SELECT *
FROM memory.apply_owner_project_component_alias_v5(
  'da200000-0000-4000-8000-000000000001',
  'da000000-0000-4000-8000-000000000001',
  :'memory_component_id',
  'Verbal Sage Memory V1/V5',
  '{"reason":"reviewed_compound_project_mention"}'::jsonb
)
\gset alias_

SELECT
  1 / ((:'alias_normalized_alias' =
    'verbal-sage-memory-v1-v5')::integer),
  1 / ((:'alias_outcome' = 'added')::integer),
  1 / ((:'alias_apply_outcome' = 'applied')::integer);

SELECT 1 / ((apply_outcome = 'replayed')::integer)
FROM memory.apply_owner_project_component_alias_v5(
  'da200000-0000-4000-8000-000000000001',
  'da000000-0000-4000-8000-000000000001',
  :'memory_component_id',
  'Verbal Sage Memory V1/V5',
  '{"reason":"reviewed_compound_project_mention"}'::jsonb
);

SELECT
  1 / ((outcome = 'existing')::integer),
  1 / ((apply_outcome = 'applied')::integer)
FROM memory.apply_owner_project_component_alias_v5(
  'da200000-0000-4000-8000-000000000002',
  'da000000-0000-4000-8000-000000000001',
  :'memory_component_id',
  'Verbal Sage Memory V1/V5',
  '{"reason":"second_review"}'::jsonb
);

SELECT pg_temp.assert_alias_call_denied(
  format(
    $sql$SELECT * FROM memory.apply_owner_project_component_alias_v5(
      'da200000-0000-4000-8000-000000000003',
      'db000000-0000-4000-8000-000000000001',
      %L::uuid,'Foreign Alias','{}'::jsonb
    )$sql$,
    :'memory_component_id'
  )
);

SELECT pg_temp.assert_alias_call_denied(
  format(
    $sql$SELECT * FROM memory.apply_owner_project_component_alias_v5(
      'da200000-0000-4000-8000-000000000001',
      'da000000-0000-4000-8000-000000000001',
      %L::uuid,'Different Alias','{}'::jsonb
    )$sql$,
    :'memory_component_id'
  )
);

SELECT pg_temp.assert_alias_call_denied(
  format(
    $sql$SELECT * FROM memory.apply_owner_project_component_alias_v5(
      'da200000-0000-4000-8000-000000000004',
      'da000000-0000-4000-8000-000000000001',
      %L::uuid,'Verbal Sage','{}'::jsonb
    )$sql$,
    :'memory_component_id'
  )
);

SELECT pg_temp.assert_alias_call_denied(
  $sql$INSERT INTO memory.project_component_alias_v5(
    owner_user_id,project_id,component_id,normalized_alias,display_alias
  ) VALUES (
    'da111111-1111-4111-8111-111111111111',
    'da000000-0000-4000-8000-000000000001',
    'da300000-0000-4000-8000-000000000001',
    'direct','Direct'
  )$sql$
);

RESET SESSION AUTHORIZATION;

SELECT
  1 / ((SELECT count(*) = 1
        FROM memory.project_component_alias_v5
        WHERE normalized_alias = 'verbal-sage-memory-v1-v5')::integer),
  1 / ((SELECT count(*) = 2
        FROM memory.project_component_alias_registration_event_v5)::integer);

ROLLBACK;
