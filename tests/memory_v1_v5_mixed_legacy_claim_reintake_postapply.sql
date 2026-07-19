\set ON_ERROR_STOP on

BEGIN READ ONLY;

SELECT set_config(
  'test.owner_a_job',
  (
    SELECT job_id::text
    FROM memory.evidence_extraction_job
    WHERE owner_user_id=:'owner_a'::uuid
      AND selector_version='20260719_v5_mixed_preference_legacy_reintake_v1'
    ORDER BY job_id
    LIMIT 1
  ),
  true
);

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_a',true);

DO $test$
BEGIN
  IF (
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE selector_version='20260719_v5_mixed_preference_legacy_reintake_v1'
      AND status='pending'
  )<>1 THEN
    RAISE EXCEPTION 'owner A does not see exactly one pending mixed reintake job';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(
      '20260719_v5_mixed_preference_legacy_reintake_v1',100,NULL
    )
  ) THEN
    RAISE EXCEPTION 'owner A mixed reintake plan was not exhausted';
  END IF;
END
$test$;

ROLLBACK;

BEGIN READ ONLY;

SELECT set_config(
  'test.owner_a_job',
  (
    SELECT job_id::text
    FROM memory.evidence_extraction_job
    WHERE owner_user_id=:'owner_a'::uuid
      AND selector_version='20260719_v5_mixed_preference_legacy_reintake_v1'
    ORDER BY job_id
    LIMIT 1
  ),
  true
);

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_b',true);

DO $test$
BEGIN
  IF (
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE selector_version='20260719_v5_mixed_preference_legacy_reintake_v1'
  )<>0 THEN
    RAISE EXCEPTION 'owner B can see owner A mixed reintake jobs';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_job
    WHERE job_id=current_setting('test.owner_a_job')::uuid
  ) THEN
    RAISE EXCEPTION 'owner B can see owner A mixed reintake job';
  END IF;
END
$test$;

ROLLBACK;
