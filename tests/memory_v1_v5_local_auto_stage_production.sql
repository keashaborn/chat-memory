\set ON_ERROR_STOP on

DO $catalog$
DECLARE
  function_owner name;
BEGIN
  IF to_regclass('memory.v5_local_packet_stage_admission') IS NULL
     OR to_regprocedure(
       'memory.register_owner_v5_local_auto_stage_v1(uuid,uuid,uuid,text,text,text,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'local auto-stage admission objects are absent';
  END IF;
  SELECT role.rolname INTO function_owner
  FROM pg_proc AS procedure
  JOIN pg_roles AS role ON role.oid=procedure.proowner
  WHERE procedure.oid=(
    'memory.register_owner_v5_local_auto_stage_v1('
    'uuid,uuid,uuid,text,text,text,text)'
  )::regprocedure;
  IF function_owner<>'memory_v5_local_disposition_maintainer'
     OR has_table_privilege(
       'brains_app','memory.v5_local_packet_stage_admission','SELECT'
     )
     OR has_table_privilege(
       'brains_app','memory.v5_local_packet_stage_admission','INSERT'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.register_owner_v5_local_auto_stage_v1(uuid,uuid,uuid,text,text,text,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'local auto-stage admission ACL is unsafe';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid='memory.v5_local_packet_stage_admission'::regclass
      AND relrowsecurity AND relforcerowsecurity
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid='memory.v5_local_packet_stage_admission'::regclass
      AND tgname='v5_local_packet_stage_admission_append_only_guard'
      AND tgenabled<>'D'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='memory.v5_local_packet_review_artifact'::regclass
      AND conname='v5_local_packet_review_artifact_owner_artifact_key'
  ) THEN
    RAISE EXCEPTION 'local auto-stage database enforcement is incomplete';
  END IF;
END
$catalog$;

BEGIN;
SET LOCAL statement_timeout='30s';
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'other_owner_user_id',true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.register_owner_v5_local_auto_stage_v1(
      'b3600000-0000-4000-8000-000000000099',
      'b3700000-0000-4000-8000-000000000099',
      'b3300000-0000-4000-8000-000000000099',
      repeat('a',64),repeat('b',64),repeat('c',64),
      'memory_v1_v5_local_auto_stage_policy_v1'
    );
    RAISE EXCEPTION 'absent cross-owner auto-stage artifact was accepted';
  EXCEPTION WHEN check_violation THEN
    NULL;
  END;
END
$isolation$;
RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_auto_stage_production: PASS' AS result;
