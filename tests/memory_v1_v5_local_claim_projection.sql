\set ON_ERROR_STOP on
BEGIN;
SET LOCAL statement_timeout='30s';

DO $catalog$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='memory'
      AND c.relname='v5_local_claim_projection_admission'
      AND c.relrowsecurity AND c.relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'local claim projection table is not forced-RLS';
  END IF;
  IF pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid=
    'memory.register_owner_v5_local_claim_projection_v1(uuid,uuid,uuid,uuid,text,text,text,text)'::regprocedure
  ))<>'memory_v5_local_projection_maintainer' THEN
    RAISE EXCEPTION 'local projection function owner is incorrect';
  END IF;
  IF has_table_privilege('brains_app',
      'memory.v5_local_claim_projection_admission','SELECT')
     OR has_table_privilege('brains_app',
      'memory.v5_local_claim_projection_admission','INSERT') THEN
    RAISE EXCEPTION 'brains_app has direct projection admission access';
  END IF;
END
$catalog$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_user_id',true);
SELECT count(*)<=1 FROM memory.plan_owner_v5_local_claim_projection_v1(1);
DO $cross_owner$
BEGIN
  PERFORM memory.register_owner_v5_local_claim_projection_v1(
    gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),
    jsonb_build_object('packet_sha256',repeat('1',64))::text,
    repeat('1',64),repeat('2',64),
    'memory_v1_v5_local_claim_projection_policy_v1'
  );
  RAISE EXCEPTION 'absent/cross-owner claim projection admitted';
EXCEPTION
  WHEN no_data_found OR check_violation OR foreign_key_violation THEN NULL;
END
$cross_owner$;
RESET SESSION AUTHORIZATION;
ROLLBACK;
SELECT 'memory_v1_v5_local_claim_projection: PASS' AS result;
