\set ON_ERROR_STOP on

DO $security$
DECLARE
  plan_oid regprocedure :=
    'memory.plan_owner_evidence_intake_v1(text,integer,uuid)';
  record_oid regprocedure :=
    'memory.record_owner_evidence_intake_terminal_v1(uuid,text,text,text,text)';
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname='memory_intake_maintainer'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'memory_intake_maintainer role is unsafe';
  END IF;
  IF (
    SELECT count(*)
    FROM pg_proc
    WHERE oid IN (plan_oid,record_oid)
      AND prosecdef
      AND proowner='memory_intake_maintainer'::regrole
      AND proconfig=ARRAY['search_path=pg_catalog']::text[]
  )<>2 THEN
    RAISE EXCEPTION 'evidence intake function ownership/config is unsafe';
  END IF;
  IF NOT has_function_privilege('brains_app',plan_oid,'EXECUTE')
     OR NOT has_function_privilege('brains_app',record_oid,'EXECUTE')
     OR EXISTS (
       SELECT 1
       FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid IN (plan_oid,record_oid)
         AND acl.grantee=0
         AND acl.privilege_type='EXECUTE'
     ) THEN
    RAISE EXCEPTION 'evidence intake function ACL is unsafe';
  END IF;
  IF has_table_privilege(
       'brains_app','memory.evidence_intake_terminal','INSERT'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_intake_terminal','UPDATE'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_intake_terminal','DELETE'
     )
     OR NOT has_table_privilege(
       'brains_app','memory.evidence_intake_terminal','SELECT'
     ) THEN
    RAISE EXCEPTION 'brains_app terminal-ledger ACL is unsafe';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_class AS relation
    WHERE relation.oid='memory.evidence_intake_terminal'::regclass
      AND relation.relrowsecurity
      AND relation.relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'terminal ledger is not protected by forced RLS';
  END IF;
END
$security$;

BEGIN;

CREATE FUNCTION pg_temp.assert_direct_terminal_insert_denied(
  p_owner uuid,
  p_evidence uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.evidence_intake_terminal(
      owner_user_id,evidence_id,selector_version,outcome,reason_code,
      decision_fingerprint,actor_user_id,invoked_by_role
    ) VALUES (
      p_owner,p_evidence,'20260716_test','empty','empty_content',
      repeat('0',64),p_owner,session_user
    );
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'brains_app directly inserted a terminal decision';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_nonterminal_record_denied(
  p_evidence uuid,
  p_hash text
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM *
    FROM memory.record_owner_evidence_intake_terminal_v1(
      p_evidence,'20260716_test',p_hash,
      'empty','upstream_completed_empty'
    );
  EXCEPTION WHEN check_violation THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'eligible evidence was recorded as terminal';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_cross_owner_record_denied(
  p_evidence uuid,
  p_hash text
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM *
    FROM memory.record_owner_evidence_intake_terminal_v1(
      p_evidence,'20260716_test',p_hash,
      'empty','upstream_completed_empty'
    );
  EXCEPTION WHEN check_violation THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'cross-owner evidence was recorded';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_terminal_append_only(
  p_terminal uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    UPDATE memory.evidence_intake_terminal
    SET details='{"changed":true}'::jsonb
    WHERE terminal_id=p_terminal;
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'terminal decision was mutable';
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','a1111111-1111-4111-8111-111111111111',true
);

SELECT evidence_id AS owner_a_empty_id
FROM memory.record_owner_evidence_v1(
  'user_statement','public.chat_log','intake-test-empty',
  NULL,'2026-07-16T17:00:00Z',1,1,
  'intake-test-empty','low','{"test":true}'::jsonb
)
\gset

SELECT evidence_id AS owner_a_eligible_id,
       content_sha256 AS owner_a_eligible_hash
FROM memory.record_owner_evidence_v1(
  'user_statement','public.chat_log','intake-test-eligible',
  'Eligible selector fixture.','2026-07-16T17:01:00Z',1,1,
  'intake-test-eligible','low','{"test":true}'::jsonb
)
\gset

SELECT evidence_id AS owner_a_completed_id,
       content_sha256 AS owner_a_completed_hash
FROM memory.record_owner_evidence_v1(
  'user_statement','public.chat_log','intake-test-completed',
  'Completed empty selector fixture.','2026-07-16T17:02:00Z',
  1,1,'intake-test-completed','low','{"test":true}'::jsonb
)
\gset

SELECT evidence_id AS owner_a_review_id,
       content_sha256 AS owner_a_review_hash
FROM memory.record_owner_evidence_v1(
  'user_statement','public.chat_log','intake-test-review',
  'Review selector fixture.','2026-07-16T17:03:00Z',
  1,1,'intake-test-review','low','{"test":true}'::jsonb
)
\gset

SELECT evidence_id AS owner_a_accounted_id
FROM memory.record_owner_evidence_v1(
  'user_statement','public.chat_log','intake-test-accounted',
  'Accounted selector fixture.','2026-07-16T17:04:00Z',
  1,1,'intake-test-accounted','low','{"test":true}'::jsonb
)
\gset

SELECT pg_temp.assert_direct_terminal_insert_denied(
  'a1111111-1111-4111-8111-111111111111',
  :'owner_a_empty_id'
);

SELECT set_config(
  'app.user_id','b2222222-2222-4222-8222-222222222222',true
);
SELECT evidence_id AS owner_b_eligible_id,
       content_sha256 AS owner_b_eligible_hash
FROM memory.record_owner_evidence_v1(
  'user_statement','public.chat_log','intake-test-owner-b',
  'Owner B selector fixture.','2026-07-16T17:05:00Z',
  1,1,'intake-test-owner-b','low','{"test":true}'::jsonb
)
\gset

RESET SESSION AUTHORIZATION;
SELECT set_config(
  'app.user_id','a1111111-1111-4111-8111-111111111111',true
);

INSERT INTO memory.consolidation_job(
  owner_user_id,source_system,source_external_id,source_sha256,
  source_recorded_at,status,pipeline_version,result
) VALUES (
  'a1111111-1111-4111-8111-111111111111',
  'public.chat_log','intake-test-completed',:'owner_a_completed_hash',
  '2026-07-16T17:02:00Z','completed','20260714_v4',
  jsonb_build_object(
    'evidence_id',:'owner_a_completed_id',
    'route','evidence_only',
    'claim_candidate_ids','[]'::jsonb,
    'preference_candidate_ids','[]'::jsonb,
    'project_candidate_ids','[]'::jsonb
  )
);

INSERT INTO memory.consolidation_job(
  owner_user_id,source_system,source_external_id,source_sha256,
  source_recorded_at,status,pipeline_version,result
) VALUES (
  'a1111111-1111-4111-8111-111111111111',
  'public.chat_log','intake-test-review',:'owner_a_review_hash',
  '2026-07-16T17:03:00Z','review_required','20260714_v4',
  jsonb_build_object(
    'evidence_id',:'owner_a_review_id',
    'route','candidate_review',
    'claim_candidate_ids','[]'::jsonb,
    'preference_candidate_ids','[]'::jsonb,
    'project_candidate_ids','[]'::jsonb
  )
);

INSERT INTO memory.candidate(
  owner_user_id,evidence_id,status,proposal,comparison,
  proposal_hash,extractor,extractor_version
) VALUES (
  'a1111111-1111-4111-8111-111111111111',
  :'owner_a_accounted_id','proposed','{}'::jsonb,'{}'::jsonb,
  repeat('a',64),'intake_test','v1'
);

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','a1111111-1111-4111-8111-111111111111',true
);

SELECT 1 / ((count(*)=4)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260716_test',100,NULL
);
SELECT 1 / ((count(*) FILTER(WHERE outcome='empty')=2)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260716_test',100,NULL
);
SELECT 1 / ((count(*) FILTER(WHERE outcome='skipped')=1)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260716_test',100,NULL
);
SELECT 1 / ((count(*) FILTER(WHERE outcome='eligible')=1)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260716_test',100,NULL
);
SELECT 1 / ((count(*) FILTER(
  WHERE evidence_id=:'owner_a_accounted_id'
)=0)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260716_test',100,NULL
);

SELECT 1 / ((apply_outcome='applied')::integer) AS applied,
       terminal_id AS recorded_terminal_id
FROM memory.record_owner_evidence_intake_terminal_v1(
  :'owner_a_empty_id','20260716_test',NULL,'empty','empty_content'
)
\gset

SELECT 1 / ((apply_outcome='replayed')::integer)
FROM memory.record_owner_evidence_intake_terminal_v1(
  :'owner_a_empty_id','20260716_test',NULL,'empty','empty_content'
);

SELECT 1 / ((count(*)=3)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260716_test',100,NULL
);

SELECT pg_temp.assert_nonterminal_record_denied(
  :'owner_a_eligible_id',:'owner_a_eligible_hash'
);
SELECT pg_temp.assert_cross_owner_record_denied(
  :'owner_b_eligible_id',:'owner_b_eligible_hash'
);

SELECT set_config(
  'app.user_id','b2222222-2222-4222-8222-222222222222',true
);
SELECT 1 / ((count(*)=1)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260716_test',100,NULL
);
SELECT 1 / ((count(*)=0)::integer)
FROM memory.evidence_intake_terminal;

RESET SESSION AUTHORIZATION;
SELECT set_config(
  'app.user_id','a1111111-1111-4111-8111-111111111111',true
);
SELECT pg_temp.assert_terminal_append_only(:'recorded_terminal_id');

ROLLBACK;

SELECT 'memory_v1_evidence_intake_selector: PASS' AS result;
