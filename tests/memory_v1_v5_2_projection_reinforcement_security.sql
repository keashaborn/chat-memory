\set ON_ERROR_STOP on
BEGIN;

DO $test$
DECLARE
  function_name text;
  function_oid regprocedure;
BEGIN
  FOREACH function_name IN ARRAY ARRAY[
    'memory.preflight_projection_reinforcement_v5_2(uuid,text)',
    'memory.stage_projection_reinforcement_v5_2(uuid,text,text)'
  ]
  LOOP
    function_oid := to_regprocedure(function_name);
    IF function_oid IS NULL THEN
      RAISE EXCEPTION 'missing reinforcement function: %',function_name;
    END IF;
    IF NOT EXISTS (
      SELECT 1
      FROM pg_proc AS procedure
      JOIN pg_roles AS owner_role ON owner_role.oid=procedure.proowner
      WHERE procedure.oid=function_oid
        AND owner_role.rolname='memory_v5_writer'
        AND procedure.prosecdef
        AND EXISTS (
          SELECT 1
          FROM unnest(procedure.proconfig) AS setting
          WHERE setting LIKE 'search_path=%'
        )
    ) THEN
      RAISE EXCEPTION 'reinforcement function ownership/security drift: %',
        function_name;
    END IF;
    IF has_function_privilege('PUBLIC',function_oid,'EXECUTE')
       OR NOT has_function_privilege('brains_app',function_oid,'EXECUTE') THEN
      RAISE EXCEPTION 'reinforcement function ACL drift: %',function_name;
    END IF;
  END LOOP;
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_writer'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'memory_v5_writer is not a restricted NOLOGIN role';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace
    WHERE namespace.nspname='memory'
      AND relation.relname IN (
        'claim','claim_revision','claim_observation',
        'projection_plan','projection_plan_item',
        'projection_claim_payload','projection_plan_observation'
      )
      AND (NOT relation.relrowsecurity OR NOT relation.relforcerowsecurity)
  ) THEN
    RAISE EXCEPTION 'reinforcement dependency lost forced RLS';
  END IF;
END
$test$;

ROLLBACK;
