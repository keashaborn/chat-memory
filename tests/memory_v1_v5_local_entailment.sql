\set ON_ERROR_STOP on

BEGIN;
SET LOCAL statement_timeout='30s';

DO $catalog$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='memory'
      AND c.relname='v5_local_entailment_assessment'
      AND c.relrowsecurity AND c.relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'local entailment table is not forced-RLS';
  END IF;
  IF pg_get_userbyid((
    SELECT proowner FROM pg_proc
    WHERE oid='memory.register_owner_v5_local_entailment_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,memory.observation_entailment_decision_v5,text,jsonb,text)'::regprocedure
  )) <> 'memory_v5_local_entailment_maintainer' THEN
    RAISE EXCEPTION 'local entailment function owner is incorrect';
  END IF;
  IF has_table_privilege('brains_app',
       'memory.v5_local_entailment_assessment','SELECT')
     OR has_table_privilege('brains_app',
       'memory.v5_local_entailment_assessment','INSERT') THEN
    RAISE EXCEPTION 'brains_app has direct entailment table access';
  END IF;
  IF (SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
             OR rolinherit OR rolbypassrls
      FROM pg_roles
      WHERE rolname='memory_v5_local_entailment_maintainer') THEN
    RAISE EXCEPTION 'local entailment maintainer is overprivileged';
  END IF;
END
$catalog$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_user_id',true);
SELECT count(*) <= 1 FROM memory.plan_owner_v5_local_entailment_v1(1);

DO $cross_owner$
BEGIN
  PERFORM memory.register_owner_v5_local_entailment_v1(
    gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),
    repeat('1',64),repeat('2',64),repeat('3',64),repeat('4',64),
    repeat('5',64),repeat('6',64),repeat('7',64),repeat('8',64),
    'entailed','high','accepted',
    'predicate_entailment_v5_1_accepted',
    jsonb_build_array(jsonb_build_object(
      'start',0,'end',1,'span_sha256',repeat('9',64)
    )),
    'memory_v1_v5_local_entailment_policy_v1'
  );
  RAISE EXCEPTION 'absent/cross-owner entailment unexpectedly admitted';
EXCEPTION
  WHEN check_violation OR no_data_found OR foreign_key_violation THEN NULL;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_entailment: PASS' AS result;
