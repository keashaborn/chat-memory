\set ON_ERROR_STOP on

BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

SELECT set_config('test.owner', :'target_owner', true);
SELECT set_config('test.other', :'other_owner', true);
SELECT set_config('test.evidence', :'evidence_id', true);
SELECT set_config('test.content', :'content_sha256', true);
SELECT set_config('test.prior_packet', :'prior_packet_id', true);
SELECT set_config('test.prior_storage', :'prior_packet_storage_sha256', true);
SELECT set_config('test.operation', :'operation_id', true);
SELECT set_config('test.job', :'job_id', true);
SELECT set_config('test.terminal', :'terminal_id', true);
SELECT set_config('test.manifest', :'manifest_sha256', true);
SELECT set_config('test.compiler', :'compiler_sha256', true);

DO $acl$
BEGIN
  IF has_table_privilege(
       'brains_app','memory.evidence_extraction_job','INSERT'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_intake_terminal','INSERT'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_extraction_event','INSERT'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1('
       'uuid,uuid,uuid,uuid,text,uuid,text,text,text)',
       'EXECUTE'
     )
     OR EXISTS (
       SELECT 1
       FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid=
         'memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1('
         'uuid,uuid,uuid,uuid,text,uuid,text,text,text)'::regprocedure
         AND acl.grantee=0
         AND acl.privilege_type='EXECUTE'
     ) THEN
    RAISE EXCEPTION
      'semantic compiler-v8 re-extraction ACL changed';
  END IF;
END
$acl$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',current_setting('test.owner'),true);

DO $apply_and_replay$
DECLARE
  first_result record;
  replay_result record;
BEGIN
  SELECT * INTO first_result
  FROM memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1(
    current_setting('test.operation')::uuid,
    current_setting('test.job')::uuid,
    current_setting('test.terminal')::uuid,
    current_setting('test.evidence')::uuid,
    current_setting('test.content'),
    current_setting('test.prior_packet')::uuid,
    current_setting('test.prior_storage'),
    current_setting('test.manifest'),
    current_setting('test.compiler')
  );
  SELECT * INTO replay_result
  FROM memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1(
    current_setting('test.operation')::uuid,
    current_setting('test.job')::uuid,
    current_setting('test.terminal')::uuid,
    current_setting('test.evidence')::uuid,
    current_setting('test.content'),
    current_setting('test.prior_packet')::uuid,
    current_setting('test.prior_storage'),
    current_setting('test.manifest'),
    current_setting('test.compiler')
  );
  IF first_result.apply_outcome<>'applied'
     OR first_result.status<>'pending'
     OR replay_result.apply_outcome<>'replayed'
     OR replay_result.job_id<>first_result.job_id
     OR replay_result.terminal_id<>first_result.terminal_id THEN
    RAISE EXCEPTION
      'semantic compiler-v8 re-extraction replay failed';
  END IF;
END
$apply_and_replay$;

DO $outside_exact_set$
BEGIN
  PERFORM *
  FROM memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1(
    gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),
    '33126656-fc5a-5fc1-a035-246b14576ee5'::uuid,
    'be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4',
    current_setting('test.prior_packet')::uuid,
    current_setting('test.prior_storage'),
    current_setting('test.manifest'),
    current_setting('test.compiler')
  );
  RAISE EXCEPTION
    'out-of-set semantic compiler-v8 re-extraction unexpectedly passed';
EXCEPTION
  WHEN SQLSTATE '23514' THEN NULL;
END
$outside_exact_set$;

SELECT set_config('app.user_id',current_setting('test.other'),true);

DO $cross_owner$
BEGIN
  PERFORM *
  FROM memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1(
    gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),
    current_setting('test.evidence')::uuid,
    current_setting('test.content'),
    current_setting('test.prior_packet')::uuid,
    current_setting('test.prior_storage'),
    current_setting('test.manifest'),
    current_setting('test.compiler')
  );
  RAISE EXCEPTION
    'cross-owner semantic compiler-v8 re-extraction unexpectedly passed';
EXCEPTION
  WHEN SQLSTATE '23514' THEN NULL;
END
$cross_owner$;

ROLLBACK;

SELECT
  'memory_v1_v5_2_semantic_compiler_v8_reextract: PASS' AS result;
