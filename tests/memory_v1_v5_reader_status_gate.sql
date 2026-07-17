\set ON_ERROR_STOP on

SELECT 1 / (((
  SELECT count(*)
  FROM pg_policies
  WHERE schemaname='memory' AND tablename='claim'
    AND policyname='surfaceable_status_v5_reader'
    AND roles=ARRAY['memory_v5_reader']::name[]
    AND permissive='RESTRICTIVE'
    AND cmd='SELECT'
)=1)::integer);

BEGIN;
SET SESSION AUTHORIZATION brains_app;

DO $missing_actor$
BEGIN
  BEGIN
    PERFORM * FROM memory.read_v5_shadow_claims(
      ARRAY['50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid]
    );
    RAISE EXCEPTION 'missing-actor status-gated read unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$missing_actor$;

SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT 1 / (((
  SELECT count(*) FROM memory.read_v5_shadow_claims(
    ARRAY['50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid]
  )
)=0)::integer);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT 1 / (((
  SELECT count(*) FROM memory.read_v5_shadow_claims(
    ARRAY['50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid]
  )
)=0)::integer);

RESET SESSION AUTHORIZATION;
UPDATE memory.claim
SET status='candidate',confidence=0.500
WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
  AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT 1 / (((
  SELECT count(*) FROM memory.read_v5_shadow_claims(
    ARRAY['50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid]
  )
)=1)::integer);
RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 1 / (((
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
    AND status='retracted' AND confidence=0
)=1)::integer);

SELECT 'memory_v1_v5_reader_status_gate: PASS' AS result;
