\set ON_ERROR_STOP on

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

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
SELECT set_config('test.provider_source', :'provider_source_sha256', true);

DO $acl$
BEGIN
  IF has_table_privilege(
       'brains_app',
       'memory.evidence_extraction_job',
       'INSERT'
     )
     OR has_table_privilege(
       'brains_app',
       'memory.evidence_intake_terminal',
       'INSERT'
     )
     OR has_table_privilege(
       'brains_app',
       'memory.evidence_extraction_event',
       'INSERT'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.enqueue_owner_v5_2_life_preference_reextract_v1('
       'uuid,uuid,uuid,uuid,text,uuid,text,text,text)',
       'EXECUTE'
     )
     OR EXISTS (
       SELECT 1
       FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid =
         'memory.enqueue_owner_v5_2_life_preference_reextract_v1('
         'uuid,uuid,uuid,uuid,text,uuid,text,text,text)'::regprocedure
         AND acl.grantee = 0
         AND acl.privilege_type = 'EXECUTE'
     ) THEN
    RAISE EXCEPTION
      'V5.2 life-preference re-extraction ACL changed';
  END IF;
END
$acl$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', current_setting('test.owner'), true);

DO $apply_and_replay$
DECLARE
  first_result record;
  replay_result record;
BEGIN
  SELECT * INTO first_result
  FROM memory.enqueue_owner_v5_2_life_preference_reextract_v1(
    current_setting('test.operation')::uuid,
    current_setting('test.job')::uuid,
    current_setting('test.terminal')::uuid,
    current_setting('test.evidence')::uuid,
    current_setting('test.content'),
    current_setting('test.prior_packet')::uuid,
    current_setting('test.prior_storage'),
    current_setting('test.manifest'),
    current_setting('test.provider_source')
  );
  SELECT * INTO replay_result
  FROM memory.enqueue_owner_v5_2_life_preference_reextract_v1(
    current_setting('test.operation')::uuid,
    current_setting('test.job')::uuid,
    current_setting('test.terminal')::uuid,
    current_setting('test.evidence')::uuid,
    current_setting('test.content'),
    current_setting('test.prior_packet')::uuid,
    current_setting('test.prior_storage'),
    current_setting('test.manifest'),
    current_setting('test.provider_source')
  );
  IF first_result.apply_outcome <> 'applied'
     OR first_result.status <> 'pending'
     OR replay_result.apply_outcome <> 'replayed'
     OR replay_result.job_id <> first_result.job_id
     OR replay_result.terminal_id <> first_result.terminal_id THEN
    RAISE EXCEPTION
      'V5.2 life-preference re-extraction replay failed';
  END IF;
END
$apply_and_replay$;

RESET SESSION AUTHORIZATION;

DO $exact_rows$
BEGIN
  IF (
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE owner_user_id = current_setting('test.owner')::uuid
      AND job_id = current_setting('test.job')::uuid
      AND evidence_id = current_setting('test.evidence')::uuid
      AND selector_version =
        '20260725_v5_2_life_preference_reextract_v1'
      AND status = 'pending'
  ) <> 1 OR (
    SELECT count(*)
    FROM memory.evidence_intake_terminal
    WHERE owner_user_id = current_setting('test.owner')::uuid
      AND terminal_id = current_setting('test.terminal')::uuid
      AND evidence_id = current_setting('test.evidence')::uuid
      AND selector_version =
        '20260725_v5_2_life_preference_reextract_v1'
  ) <> 1 OR (
    SELECT count(*)
    FROM memory.evidence_extraction_event
    WHERE owner_user_id = current_setting('test.owner')::uuid
      AND job_id = current_setting('test.job')::uuid
      AND operation_id = current_setting('test.operation')::uuid
      AND event_type = 'queued'
  ) <> 1 THEN
    RAISE EXCEPTION
      'V5.2 life-preference re-extraction row bounds changed';
  END IF;
END
$exact_rows$;

SET LOCAL SESSION AUTHORIZATION brains_app;

DO $outside_exact_set$
BEGIN
  PERFORM *
  FROM memory.enqueue_owner_v5_2_life_preference_reextract_v1(
    gen_random_uuid(),
    gen_random_uuid(),
    gen_random_uuid(),
    gen_random_uuid(),
    current_setting('test.content'),
    current_setting('test.prior_packet')::uuid,
    current_setting('test.prior_storage'),
    current_setting('test.manifest'),
    current_setting('test.provider_source')
  );
  RAISE EXCEPTION
    'out-of-set V5.2 life-preference re-extraction passed';
EXCEPTION
  WHEN SQLSTATE '22023' THEN NULL;
END
$outside_exact_set$;

SELECT set_config('app.user_id', current_setting('test.other'), true);

DO $cross_owner$
BEGIN
  PERFORM *
  FROM memory.enqueue_owner_v5_2_life_preference_reextract_v1(
    gen_random_uuid(),
    gen_random_uuid(),
    gen_random_uuid(),
    current_setting('test.evidence')::uuid,
    current_setting('test.content'),
    current_setting('test.prior_packet')::uuid,
    current_setting('test.prior_storage'),
    current_setting('test.manifest'),
    current_setting('test.provider_source')
  );
  RAISE EXCEPTION
    'cross-owner V5.2 life-preference re-extraction passed';
EXCEPTION
  WHEN SQLSTATE '23514' THEN NULL;
END
$cross_owner$;

ROLLBACK;

SELECT 'memory_v1_v5_2_life_preference_reextract: PASS' AS result;
