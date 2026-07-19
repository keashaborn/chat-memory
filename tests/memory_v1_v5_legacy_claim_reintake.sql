\set ON_ERROR_STOP on

BEGIN;

DO $test$
DECLARE
  function_oid regprocedure;
BEGIN
  FOREACH function_oid IN ARRAY ARRAY[
    'memory.plan_owner_v5_legacy_claim_reintake_v1(text,integer,uuid)'::regprocedure,
    'memory.enqueue_owner_v5_legacy_claim_reintake_v1(uuid,text,text,text,text)'::regprocedure
  ] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_proc
      WHERE oid=function_oid AND prosecdef
        AND proconfig=ARRAY['search_path=pg_catalog']::text[]
    ) THEN
      RAISE EXCEPTION 'legacy reintake function is not fail-closed: %',function_oid;
    END IF;
    IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE') THEN
      RAISE EXCEPTION 'brains_app cannot execute %',function_oid;
    END IF;
  END LOOP;
END
$test$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_a',true);
SELECT set_config('test.owner_b',:'owner_b',true);

DO $test$
DECLARE
  planned integer;
  cross_owner_evidence uuid;
BEGIN
  SELECT count(*) INTO planned
  FROM memory.plan_owner_v5_legacy_claim_reintake_v1(
    '20260719_v5_legacy_claim_reintake_v1',100,NULL
  );
  IF planned<1 THEN
    RAISE EXCEPTION 'owner A has no legacy-only migration evidence';
  END IF;

  SELECT evidence_id INTO cross_owner_evidence
  FROM memory.evidence
  WHERE owner_user_id=current_setting('test.owner_b')::uuid
  ORDER BY evidence_id LIMIT 1;
  IF EXISTS (
    SELECT 1 FROM memory.plan_owner_v5_legacy_claim_reintake_v1(
      '20260719_v5_legacy_claim_reintake_v1',1,cross_owner_evidence
    )
  ) THEN
    RAISE EXCEPTION 'cross-owner legacy evidence was visible';
  END IF;
END
$test$;

ROLLBACK;
