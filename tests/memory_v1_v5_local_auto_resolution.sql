\set ON_ERROR_STOP on

BEGIN;
SET LOCAL statement_timeout='30s';

DO $catalog$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='memory'
      AND c.relname='v5_local_auto_resolution_admission'
      AND c.relrowsecurity AND c.relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'local auto-resolution admission is not forced-RLS';
  END IF;
  IF pg_get_userbyid((
    SELECT proowner FROM pg_proc
    WHERE oid='memory.register_owner_v5_local_auto_resolution_v1(uuid,uuid,uuid,uuid,uuid,uuid,text,text,text)'::regprocedure
  )) <> 'memory_v5_local_disposition_maintainer' THEN
    RAISE EXCEPTION 'local auto-resolution register owner is incorrect';
  END IF;
  IF has_table_privilege('brains_app',
       'memory.v5_local_auto_resolution_admission','SELECT')
     OR has_table_privilege('brains_app',
       'memory.v5_local_auto_resolution_admission','INSERT') THEN
    RAISE EXCEPTION 'brains_app has direct auto-resolution table access';
  END IF;
END
$catalog$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_user_id',true);
SELECT count(*) <= 1
FROM memory.plan_owner_v5_local_auto_resolution_v1(1);

DO $cross_owner$
BEGIN
  PERFORM memory.register_owner_v5_local_auto_resolution_v1(
    gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),
    gen_random_uuid(),gen_random_uuid(),repeat('1',64),repeat('2',64),
    'memory_v1_v5_local_auto_resolution_policy_v1'
  );
  RAISE EXCEPTION 'absent/cross-owner resolution unexpectedly admitted';
EXCEPTION
  WHEN check_violation OR no_data_found OR foreign_key_violation THEN NULL;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_auto_resolution: PASS' AS result;
