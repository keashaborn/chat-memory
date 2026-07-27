\set ON_ERROR_STOP on

BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

SELECT set_config('test.owner', :'target_owner', true);
SELECT set_config('test.other', :'other_owner', true);
SELECT set_config('test.manifest', :'manifest_sha256', true);

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
       'memory.enqueue_owner_v5_2_contextual_pet_reextract_v1(text)',
       'EXECUTE'
     )
     OR EXISTS (
       SELECT 1
       FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid=
         'memory.enqueue_owner_v5_2_contextual_pet_reextract_v1(text)'
           ::regprocedure
         AND acl.grantee=0
         AND acl.privilege_type='EXECUTE'
     ) THEN
    RAISE EXCEPTION
      'contextual pet re-extraction ACL changed';
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
  FROM memory.enqueue_owner_v5_2_contextual_pet_reextract_v1(
    current_setting('test.manifest')
  );
  SELECT * INTO replay_result
  FROM memory.enqueue_owner_v5_2_contextual_pet_reextract_v1(
    current_setting('test.manifest')
  );
  IF first_result.apply_outcome<>'applied'
     OR first_result.job_count<>5
     OR first_result.terminal_count<>5
     OR first_result.event_count<>5
     OR replay_result.apply_outcome<>'replayed'
     OR replay_result.job_count<>5
     OR replay_result.terminal_count<>5
     OR replay_result.event_count<>5 THEN
    RAISE EXCEPTION
      'contextual pet re-extraction apply or replay failed';
  END IF;
END
$apply_and_replay$;

RESET SESSION AUTHORIZATION;

DO $exact_rows$
BEGIN
  IF (
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE owner_user_id=current_setting('test.owner')::uuid
      AND selector_version=
        '20260727_v5_2_contextual_pet_reextract_v1'
      AND status='pending'
      AND attempts=0
      AND route='relational_extraction'
  )<>5
  OR (
    SELECT count(*)
    FROM memory.evidence_intake_terminal
    WHERE owner_user_id=current_setting('test.owner')::uuid
      AND selector_version=
        '20260727_v5_2_contextual_pet_reextract_v1'
      AND outcome='dispatched'
      AND reason_code='eligible_dispatched'
      AND details->>'manifest_sha256'=current_setting('test.manifest')
      AND details->>'source_prose_copied'='false'
  )<>5
  OR (
    SELECT count(*)
    FROM memory.evidence_extraction_event AS event
    JOIN memory.evidence_extraction_job AS job
      ON job.owner_user_id=event.owner_user_id
     AND job.job_id=event.job_id
    WHERE event.owner_user_id=current_setting('test.owner')::uuid
      AND job.selector_version=
        '20260727_v5_2_contextual_pet_reextract_v1'
      AND event.event_type='queued'
      AND event.from_status IS NULL
      AND event.to_status='pending'
  )<>5
  OR (
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE owner_user_id=current_setting('test.owner')::uuid
      AND job_id IN (
        '8b1b36c4-2a7b-43a3-bdbb-97d17749e1f6'::uuid,
        '1349d208-d1b4-476d-8379-f49c84870543'::uuid,
        'a4d6a327-372c-4776-9411-bcb1b58ac121'::uuid,
        '079e5f54-e0c3-4051-976c-a47593a3ab6b'::uuid,
        '13ac1f7e-a09e-426e-98e8-d963d81f02a8'::uuid
      )
      AND (
        (job_id='8b1b36c4-2a7b-43a3-bdbb-97d17749e1f6'::uuid
          AND status='review_required' AND attempts=2)
        OR
        (job_id<>'8b1b36c4-2a7b-43a3-bdbb-97d17749e1f6'::uuid
          AND status='pending' AND attempts=1)
      )
  )<>5 THEN
    RAISE EXCEPTION
      'contextual pet re-extraction exact row proof failed';
  END IF;
END
$exact_rows$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',current_setting('test.other'),true);

DO $cross_owner$
BEGIN
  PERFORM *
  FROM memory.enqueue_owner_v5_2_contextual_pet_reextract_v1(
    current_setting('test.manifest')
  );
  RAISE EXCEPTION
    'cross-owner contextual pet re-extraction unexpectedly passed';
EXCEPTION
  WHEN SQLSTATE '23514' THEN NULL;
END
$cross_owner$;

SELECT set_config('app.user_id',current_setting('test.owner'),true);

DO $wrong_manifest$
BEGIN
  PERFORM *
  FROM memory.enqueue_owner_v5_2_contextual_pet_reextract_v1(
    repeat('0',64)
  );
  RAISE EXCEPTION
    'wrong-manifest contextual pet re-extraction unexpectedly passed';
EXCEPTION
  WHEN SQLSTATE '23514' THEN NULL;
END
$wrong_manifest$;

ROLLBACK;

SELECT
  'memory_v1_v5_2_contextual_pet_reextract_v1: PASS' AS result;
