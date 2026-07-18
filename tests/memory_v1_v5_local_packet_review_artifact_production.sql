\set ON_ERROR_STOP on

DO $catalog$
DECLARE
  policy_roles oid[];
  artifact_owner name;
  function_owner name;
BEGIN
  IF to_regclass('memory.v5_local_packet_review_artifact') IS NULL
     OR to_regprocedure(
       'memory.record_owner_v5_local_review_artifact_v1(uuid,uuid,uuid,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'local review artifact objects are absent';
  END IF;
  SELECT role.rolname INTO artifact_owner
  FROM pg_class AS relation
  JOIN pg_roles AS role ON role.oid=relation.relowner
  WHERE relation.oid='memory.v5_local_packet_review_artifact'::regclass;
  SELECT role.rolname INTO function_owner
  FROM pg_proc AS procedure
  JOIN pg_roles AS role ON role.oid=procedure.proowner
  WHERE procedure.oid=(
    'memory.record_owner_v5_local_review_artifact_v1('
    'uuid,uuid,uuid,text,uuid,uuid,text,text,text,'
    'integer,integer,integer,integer,integer)'
  )::regprocedure;
  IF artifact_owner<>'sage'
     OR function_owner<>'memory_v5_local_disposition_maintainer' THEN
    RAISE EXCEPTION 'local review artifact ownership is unsafe';
  END IF;
  IF has_table_privilege(
       'brains_app','memory.v5_local_packet_review_artifact','SELECT'
     )
     OR has_table_privilege(
       'brains_app','memory.v5_local_packet_review_artifact','INSERT'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.record_owner_v5_local_review_artifact_v1(uuid,uuid,uuid,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'local review artifact ACL is unsafe';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid='memory.v5_local_packet_review_artifact'::regclass
      AND relrowsecurity AND relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'local review artifact RLS is not forced';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid='memory.v5_local_packet_review_artifact'::regclass
      AND tgname='v5_local_packet_review_artifact_append_only_guard'
      AND tgenabled<>'D'
  ) THEN
    RAISE EXCEPTION 'local review artifact append-only guard is absent';
  END IF;
  SELECT polroles INTO policy_roles FROM pg_policy
  WHERE polrelid='memory.relational_stage_batch'::regclass
    AND polname='owner_isolation';
  IF cardinality(policy_roles)<>3
     OR NOT ('memory_v5_writer'::regrole::oid=ANY(policy_roles))
     OR NOT ('memory_v5_local_disposition_maintainer'::regrole::oid=ANY(policy_roles))
     OR NOT ('memory_v5_local_review_reader'::regrole::oid=ANY(policy_roles)) THEN
    RAISE EXCEPTION 'stage policy lacks exact local review roles';
  END IF;
END
$catalog$;

BEGIN;
SET LOCAL statement_timeout='30s';
SELECT set_config('test.foreign_packet_id',:'foreign_packet_id',true);
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'other_owner_user_id',true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.record_owner_v5_local_review_artifact_v1(
      '41000000-0000-4000-8000-000000000001',
      '41000000-0000-4000-8000-000000000002',
      current_setting('test.foreign_packet_id')::uuid,
      repeat('a',64),
      '41000000-0000-4000-8000-000000000003',
      '41000000-0000-4000-8000-000000000004',
      repeat('b',64),repeat('c',64),repeat('d',40),
      0,1,0,0,0
    );
    RAISE EXCEPTION 'cross-owner local review artifact unexpectedly succeeded';
  EXCEPTION WHEN check_violation THEN
    NULL;
  END;
END
$isolation$;
RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_packet_review_artifact_production: PASS' AS result;
