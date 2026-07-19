\set ON_ERROR_STOP on
BEGIN;
SET LOCAL statement_timeout='30s';

DO $catalog$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'memory.v5_local_entity_validation_assessment'::regclass,
    'memory.v5_local_validated_stage_admission'::regclass
  ] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_class
      WHERE oid=target AND relrowsecurity AND relforcerowsecurity
    ) THEN
      RAISE EXCEPTION 'local entity validation table is not forced-RLS';
    END IF;
  END LOOP;
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_local_entity_validation_maintainer'
      AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
           OR rolinherit OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'local entity validation maintainer is overprivileged';
  END IF;
  IF has_table_privilege('brains_app',
       'memory.v5_local_entity_validation_assessment','SELECT')
     OR has_table_privilege('brains_app',
       'memory.v5_local_entity_validation_assessment','INSERT')
     OR has_table_privilege('brains_app',
       'memory.v5_local_validated_stage_admission','SELECT') THEN
    RAISE EXCEPTION 'brains_app has direct local entity validation access';
  END IF;
  IF position(
       'stage.decision IN (''auto_stage_eligible'',''validated_entity_stage'')'
       IN pg_get_functiondef(
         'memory.plan_owner_v5_local_entailment_v1(integer)'::regprocedure
       )
     )=0
     OR position(
       'source.stage_decision NOT IN (''auto_stage_eligible'',''validated_entity_stage'')'
       IN pg_get_functiondef(
         'memory.register_owner_v5_local_entailment_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,memory.observation_entailment_decision_v5,text,jsonb,text)'::regprocedure
       )
     )=0 THEN
    RAISE EXCEPTION 'validated-stage entailment compatibility is absent';
  END IF;
END
$catalog$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_user_id',true);
SELECT count(*)<=1 FROM memory.plan_owner_v5_local_entity_validation_v1(1);
DO $cross_owner$
BEGIN
  PERFORM memory.register_owner_v5_local_entity_validation_v1(
    gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),
    'e01',repeat('1',64),repeat('2',64),repeat('3',64),repeat('4',64),
    repeat('5',64),repeat('6',64),repeat('7',64),repeat('8',64),
    repeat('9',64),repeat('a',64),'supported','high','accepted',
    'explicit_named_entity_supported',
    '[{"start":0,"end":1,"span_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}]'::jsonb,
    'memory_v1_v5_local_entity_validation_policy_v1'
  );
  RAISE EXCEPTION 'absent/cross-owner entity validation admitted';
EXCEPTION
  WHEN no_data_found OR check_violation OR foreign_key_violation THEN NULL;
END
$cross_owner$;
RESET SESSION AUTHORIZATION;

ROLLBACK;
SELECT 'memory_v1_v5_local_entity_validation: PASS' AS result;
