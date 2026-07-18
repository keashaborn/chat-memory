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
  ) THEN
    RAISE EXCEPTION 'local auto-stage RLS or append-only guard is absent';
  END IF;
END
$catalog$;

BEGIN;
SET LOCAL statement_timeout='30s';
SELECT set_config('test.artifact_id',:'artifact_id',true);
SELECT set_config('test.review_sha',:'review_report_sha256',true);
SELECT set_config('test.bundle_sha',:'stage_bundle_sha256',true);
SELECT set_config('test.packet_sha',:'packet_storage_sha256',true);
ALTER TABLE memory.v5_local_packet_review_artifact DISABLE TRIGGER USER;
UPDATE memory.v5_local_packet_review_artifact
SET auto_link_count=0,manual_review_count=1
WHERE owner_user_id=:'owner_user_id'::uuid
  AND artifact_id=:'artifact_id'::uuid;
ALTER TABLE memory.v5_local_packet_review_artifact ENABLE TRIGGER USER;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_user_id',true);
DO $manual_block$
BEGIN
  IF EXISTS (SELECT 1 FROM memory.plan_owner_v5_local_auto_stage_v1(1)) THEN
    RAISE EXCEPTION 'manual-review artifact entered the automatic plan';
  END IF;
  BEGIN
    PERFORM * FROM memory.register_owner_v5_local_auto_stage_v1(
      'a3600000-0000-4000-8000-000000000010',
      'a3700000-0000-4000-8000-000000000010',
      current_setting('test.artifact_id')::uuid,
      current_setting('test.review_sha'),current_setting('test.bundle_sha'),
      current_setting('test.packet_sha'),
      'memory_v1_v5_local_auto_stage_policy_v1'
    );
    RAISE EXCEPTION 'manual-review artifact was automatically admitted';
  EXCEPTION WHEN check_violation THEN
    NULL;
  END;
END
$manual_block$;
RESET SESSION AUTHORIZATION;
ROLLBACK;

BEGIN;
SET LOCAL statement_timeout='30s';
SELECT set_config('test.artifact_id',:'artifact_id',true);
SELECT set_config('test.review_sha',:'review_report_sha256',true);
SELECT set_config('test.bundle_sha',:'stage_bundle_sha256',true);
SELECT set_config('test.packet_sha',:'packet_storage_sha256',true);
SET SESSION AUTHORIZATION brains_app;

DO $missing_actor$
BEGIN
  BEGIN
    PERFORM * FROM memory.plan_owner_v5_local_auto_stage_v1(1);
    RAISE EXCEPTION 'missing actor unexpectedly planned auto-stage work';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END
$missing_actor$;

SELECT set_config('app.user_id',:'owner_user_id',true);
DO $plan_and_apply$
DECLARE
  planned record;
  result record;
BEGIN
  SELECT * INTO planned FROM memory.plan_owner_v5_local_auto_stage_v1(1);
  IF planned.artifact_id<>current_setting('test.artifact_id')::uuid THEN
    RAISE EXCEPTION 'eligible artifact was not planned';
  END IF;
  SELECT * INTO result FROM memory.register_owner_v5_local_auto_stage_v1(
    'a3600000-0000-4000-8000-000000000001',
    'a3700000-0000-4000-8000-000000000001',
    current_setting('test.artifact_id')::uuid,
    current_setting('test.review_sha'),current_setting('test.bundle_sha'),
    current_setting('test.packet_sha'),
    'memory_v1_v5_local_auto_stage_policy_v1'
  );
  IF result.apply_outcome<>'applied'
     OR result.decision<>'auto_stage_eligible' THEN
    RAISE EXCEPTION 'eligible artifact was not admitted';
  END IF;
  SELECT * INTO result FROM memory.register_owner_v5_local_auto_stage_v1(
    'a3600000-0000-4000-8000-000000000001',
    'a3700000-0000-4000-8000-000000000001',
    current_setting('test.artifact_id')::uuid,
    current_setting('test.review_sha'),current_setting('test.bundle_sha'),
    current_setting('test.packet_sha'),
    'memory_v1_v5_local_auto_stage_policy_v1'
  );
  IF result.apply_outcome<>'replayed' THEN
    RAISE EXCEPTION 'auto-stage admission replay wrote twice';
  END IF;
  IF EXISTS (SELECT 1 FROM memory.plan_owner_v5_local_auto_stage_v1(1)) THEN
    RAISE EXCEPTION 'admitted artifact remained in the planner';
  END IF;
END
$plan_and_apply$;

SELECT set_config('app.user_id',:'other_owner_user_id',true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.register_owner_v5_local_auto_stage_v1(
      'b3600000-0000-4000-8000-000000000001',
      'b3700000-0000-4000-8000-000000000001',
      current_setting('test.artifact_id')::uuid,
      current_setting('test.review_sha'),current_setting('test.bundle_sha'),
      current_setting('test.packet_sha'),
      'memory_v1_v5_local_auto_stage_policy_v1'
    );
    RAISE EXCEPTION 'cross-owner auto-stage admission unexpectedly succeeded';
  EXCEPTION WHEN check_violation THEN
    NULL;
  END;
END
$isolation$;

RESET SESSION AUTHORIZATION;
ROLLBACK;
SELECT 'memory_v1_v5_local_auto_stage_admission: PASS' AS result;
