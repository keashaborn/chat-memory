\set ON_ERROR_STOP on

DO $catalog$
DECLARE
  policy_roles oid[];
BEGIN
  IF to_regclass('memory.v5_local_packet_review_artifact') IS NULL
     OR to_regprocedure(
       'memory.record_owner_v5_local_review_artifact_v1(uuid,uuid,uuid,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'local review artifact objects are absent';
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
SELECT set_config('test.packet_id',:'packet_id',true);
SELECT set_config('test.packet_storage_sha256',:'packet_storage_sha256',true);
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_user_id',true);

DO $apply$
DECLARE
  result record;
BEGIN
  SELECT * INTO result FROM memory.record_owner_v5_local_review_artifact_v1(
    '30000000-0000-4000-8000-000000000001',
    '30000000-0000-4000-8000-000000000002',
    current_setting('test.packet_id')::uuid,
    current_setting('test.packet_storage_sha256'),
    '30000000-0000-4000-8000-000000000003',
    '30000000-0000-4000-8000-000000000004',
    repeat('a',64),repeat('b',64),repeat('c',40),
    :auto_link_count,:manual_review_count,:deferred_count,:rejected_count,
    :blocking_code_count
  );
  IF result.apply_outcome<>'applied'
     OR result.review_disposition<>'manual_review_required' THEN
    RAISE EXCEPTION 'local review artifact was not applied';
  END IF;
  SELECT * INTO result FROM memory.record_owner_v5_local_review_artifact_v1(
    '30000000-0000-4000-8000-000000000001',
    '30000000-0000-4000-8000-000000000002',
    current_setting('test.packet_id')::uuid,
    current_setting('test.packet_storage_sha256'),
    '30000000-0000-4000-8000-000000000003',
    '30000000-0000-4000-8000-000000000004',
    repeat('a',64),repeat('b',64),repeat('c',40),
    :auto_link_count,:manual_review_count,:deferred_count,:rejected_count,
    :blocking_code_count
  );
  IF result.apply_outcome<>'replayed' THEN
    RAISE EXCEPTION 'local review artifact replay wrote twice';
  END IF;
END
$apply$;

DO $excluded$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
    WHERE packet_id=current_setting('test.packet_id')::uuid
  ) THEN
    RAISE EXCEPTION 'reviewed packet remained in local route plan';
  END IF;
END
$excluded$;

SELECT set_config('app.user_id',:'other_owner_user_id',true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.record_owner_v5_local_review_artifact_v1(
      '40000000-0000-4000-8000-000000000001',
      '40000000-0000-4000-8000-000000000002',
      current_setting('test.packet_id')::uuid,
      current_setting('test.packet_storage_sha256'),
      '40000000-0000-4000-8000-000000000003',
      '40000000-0000-4000-8000-000000000004',
      repeat('d',64),repeat('e',64),repeat('f',40),
      :auto_link_count,:manual_review_count,:deferred_count,:rejected_count,
      :blocking_code_count
    );
    RAISE EXCEPTION 'cross-owner local review artifact unexpectedly succeeded';
  EXCEPTION WHEN check_violation THEN
    NULL;
  END;
END
$isolation$;

RESET SESSION AUTHORIZATION;
ROLLBACK;
SELECT 'memory_v1_v5_local_packet_review_artifact: PASS' AS result;
