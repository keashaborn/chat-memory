\set ON_ERROR_STOP on

BEGIN;

DO $test$
DECLARE
  function_oid regprocedure;
BEGIN
  FOREACH function_oid IN ARRAY ARRAY[
    'memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(text,integer,uuid)'::regprocedure,
    'memory.enqueue_owner_v5_mixed_legacy_claim_reintake_v1(uuid,text,text,text,text)'::regprocedure
  ] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_proc
      WHERE oid=function_oid AND prosecdef
        AND proconfig=ARRAY['search_path=pg_catalog']::text[]
    ) THEN
      RAISE EXCEPTION 'mixed reintake function is not fail-closed: %',function_oid;
    END IF;
    IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE') THEN
      RAISE EXCEPTION 'brains_app cannot execute %',function_oid;
    END IF;
  END LOOP;
END
$test$;

SELECT set_config(
  'test.preference_signature',
  (
    SELECT encode(public.digest(convert_to(coalesce(string_agg(value,E'\n'
      ORDER BY value),''),'UTF8'),'sha256'),'hex')
    FROM (
      SELECT 'head|' || to_jsonb(head)::text AS value
      FROM memory.user_preference AS head
      WHERE head.owner_user_id=:'owner_a'::uuid
      UNION ALL
      SELECT 'revision|' || to_jsonb(revision)::text
      FROM memory.preference_revision AS revision
      WHERE revision.owner_user_id=:'owner_a'::uuid
      UNION ALL
      SELECT 'evidence|' || to_jsonb(link)::text
      FROM memory.preference_revision_evidence AS link
      WHERE link.owner_user_id=:'owner_a'::uuid
    ) AS values
  ),
  true
);
SELECT set_config(
  'test.cross_owner_evidence',
  (
    SELECT evidence_id::text
    FROM memory.evidence
    WHERE owner_user_id=:'owner_b'::uuid
    ORDER BY evidence_id
    LIMIT 1
  ),
  true
);
SELECT set_config('test.owner_b',:'owner_b',true);
SELECT set_config('test.owner_a',:'owner_a',true);

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_a',true);

DO $test$
DECLARE
  planned record;
  first_apply record;
  replay_apply record;
BEGIN
  SELECT * INTO planned
  FROM memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(
    '20260719_v5_mixed_preference_legacy_reintake_v1',100,NULL
  );
  IF NOT FOUND
     OR planned.outcome<>'eligible'
     OR planned.route<>'relational_extraction'
     OR planned.reason_code<>'eligible_unprocessed'
     OR planned.legacy_claim_count<>5
     OR planned.active_preference_count<>2 THEN
    RAISE EXCEPTION 'owner A mixed reintake plan changed';
  END IF;
  IF (
    SELECT count(*)
    FROM memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(
      '20260719_v5_mixed_preference_legacy_reintake_v1',100,NULL
    )
  )<>1 THEN
    RAISE EXCEPTION 'owner A mixed reintake plan is not exactly one row';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(
      '20260719_v5_mixed_preference_legacy_reintake_v1',1,
      current_setting('test.cross_owner_evidence')::uuid
    )
  ) THEN
    RAISE EXCEPTION 'cross-owner evidence was visible to mixed reintake plan';
  END IF;

  SELECT * INTO first_apply
  FROM memory.enqueue_owner_v5_mixed_legacy_claim_reintake_v1(
    planned.evidence_id,
    '20260719_v5_mixed_preference_legacy_reintake_v1',
    planned.evidence_content_sha256,
    planned.route,
    planned.reason_code
  );
  SELECT * INTO replay_apply
  FROM memory.enqueue_owner_v5_mixed_legacy_claim_reintake_v1(
    planned.evidence_id,
    '20260719_v5_mixed_preference_legacy_reintake_v1',
    planned.evidence_content_sha256,
    planned.route,
    planned.reason_code
  );
  IF first_apply.apply_outcome<>'applied'
     OR replay_apply.apply_outcome<>'replayed'
     OR first_apply.job_id<>replay_apply.job_id
     OR first_apply.intake_terminal_id<>replay_apply.intake_terminal_id THEN
    RAISE EXCEPTION 'mixed reintake apply/replay changed';
  END IF;
  IF (
    SELECT count(*) FROM memory.evidence_extraction_job
    WHERE selector_version='20260719_v5_mixed_preference_legacy_reintake_v1'
  )<>1 THEN
    RAISE EXCEPTION 'mixed reintake did not create exactly one job';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(
      '20260719_v5_mixed_preference_legacy_reintake_v1',100,NULL
    )
  ) THEN
    RAISE EXCEPTION 'mixed reintake plan was not exhausted';
  END IF;

  PERFORM set_config('app.user_id',current_setting('test.owner_b'),true);
  IF EXISTS (
    SELECT 1 FROM memory.evidence_extraction_job
    WHERE selector_version='20260719_v5_mixed_preference_legacy_reintake_v1'
  ) THEN
    RAISE EXCEPTION 'owner B can see owner A mixed reintake job';
  END IF;
END
$test$;

RESET SESSION AUTHORIZATION;

DO $test$
DECLARE
  current_signature text;
BEGIN
  SELECT encode(public.digest(convert_to(coalesce(string_agg(value,E'\n'
    ORDER BY value),''),'UTF8'),'sha256'),'hex') INTO current_signature
  FROM (
    SELECT 'head|' || to_jsonb(head)::text AS value
    FROM memory.user_preference AS head
    WHERE head.owner_user_id=current_setting('test.owner_a')::uuid
    UNION ALL
    SELECT 'revision|' || to_jsonb(revision)::text
    FROM memory.preference_revision AS revision
    WHERE revision.owner_user_id=current_setting('test.owner_a')::uuid
    UNION ALL
    SELECT 'evidence|' || to_jsonb(link)::text
    FROM memory.preference_revision_evidence AS link
    WHERE link.owner_user_id=current_setting('test.owner_a')::uuid
  ) AS values;
  IF current_signature<>current_setting('test.preference_signature') THEN
    RAISE EXCEPTION 'mixed reintake modified the preference lane';
  END IF;
END
$test$;

ROLLBACK;
