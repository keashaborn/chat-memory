\set ON_ERROR_STOP on

DO $catalog$
BEGIN
  IF to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_local_packet_disposition_v1(integer)'
     ) IS NULL
     OR to_regprocedure(
       'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'local packet disposition objects are absent';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_local_disposition_maintainer'
      AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolinherit AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'local packet disposition maintainer role is unsafe';
  END IF;
  IF has_table_privilege('brains_app','memory.v5_local_packet_disposition','SELECT')
     OR has_table_privilege('brains_app','memory.v5_local_packet_disposition','INSERT')
     OR has_table_privilege('brains_app','memory.v5_local_packet_disposition','UPDATE')
     OR has_table_privilege('brains_app','memory.v5_local_packet_disposition','DELETE')
     OR NOT has_function_privilege(
       'brains_app','memory.plan_owner_v5_local_packet_disposition_v1(integer)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'local packet disposition ACL is unsafe';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid='memory.v5_local_packet_disposition'::regclass
      AND relrowsecurity AND relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'local packet disposition RLS is not forced';
  END IF;
END
$catalog$;

BEGIN;
SET LOCAL statement_timeout='30s';
SELECT set_config('test.packet_id',:'packet_id',true);
SELECT set_config('test.packet_storage_sha256',:'packet_storage_sha256',true);
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_user_id',true);

DO $owner_plan$
BEGIN
  IF (SELECT count(*) FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
      WHERE packet_id=current_setting('test.packet_id')::uuid
        AND disposition_route='terminal_deferral'
        AND reason_code='deferral_only_no_stage')<>1 THEN
    RAISE EXCEPTION 'owner terminal-deferral plan is absent';
  END IF;
END
$owner_plan$;

DO $apply$
DECLARE
  result record;
BEGIN
  SELECT * INTO result FROM memory.finalize_owner_v5_local_deferral_v1(
    '10000000-0000-4000-8000-000000000001',
    '10000000-0000-4000-8000-000000000002',
    current_setting('test.packet_id')::uuid,
    current_setting('test.packet_storage_sha256'),
    'deferral_only_no_stage'
  );
  IF result.apply_outcome<>'applied'
     OR result.disposition<>'terminal_no_stage' THEN
    RAISE EXCEPTION 'terminal deferral was not applied';
  END IF;
  SELECT * INTO result FROM memory.finalize_owner_v5_local_deferral_v1(
    '10000000-0000-4000-8000-000000000001',
    '10000000-0000-4000-8000-000000000002',
    current_setting('test.packet_id')::uuid,
    current_setting('test.packet_storage_sha256'),
    'deferral_only_no_stage'
  );
  IF result.apply_outcome<>'replayed' THEN
    RAISE EXCEPTION 'terminal deferral replay wrote again';
  END IF;
END
$apply$;

DO $removed_from_plan$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
    WHERE packet_id=current_setting('test.packet_id')::uuid
  ) THEN
    RAISE EXCEPTION 'terminal packet remained in the disposition plan';
  END IF;
END
$removed_from_plan$;

SELECT set_config('app.user_id',:'other_owner_user_id',true);
DO $isolation$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
    WHERE packet_id=current_setting('test.packet_id')::uuid
  ) THEN
    RAISE EXCEPTION 'cross-owner packet appeared in disposition plan';
  END IF;
  BEGIN
    PERFORM * FROM memory.finalize_owner_v5_local_deferral_v1(
      '20000000-0000-4000-8000-000000000001',
      '20000000-0000-4000-8000-000000000002',
      current_setting('test.packet_id')::uuid,
      current_setting('test.packet_storage_sha256'),
      'deferral_only_no_stage'
    );
    RAISE EXCEPTION 'cross-owner disposition unexpectedly succeeded';
  EXCEPTION WHEN check_violation THEN
    NULL;
  END;
END
$isolation$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_packet_disposition: PASS' AS result;
