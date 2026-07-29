\set ON_ERROR_STOP on

DO $catalog$
DECLARE
  selector regprocedure :=
    'memory.select_owner_contextual_generation_v1(uuid,integer)'::regprocedure;
BEGIN
  IF (SELECT rolname <> 'memory_intake_maintainer'
      FROM pg_proc
      JOIN pg_roles ON pg_roles.oid = pg_proc.proowner
      WHERE pg_proc.oid = selector)
     OR NOT (SELECT prosecdef FROM pg_proc WHERE oid = selector)
     OR NOT has_function_privilege('brains_app', selector, 'EXECUTE')
     OR has_table_privilege(
       'brains_app',
       'memory.evidence_contextual_span_v2',
       'SELECT'
     ) THEN
    RAISE EXCEPTION 'context generation selector ACL is unsafe';
  END IF;
END
$catalog$;

BEGIN;
SET LOCAL statement_timeout = '30s';
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'target_owner', true);

SELECT 1 / ((count(*) = :'expected_count'::integer)::integer)
FROM memory.select_owner_contextual_generation_v1(
  :'target_evidence'::uuid,
  32
)
WHERE owner_user_id = :'target_owner'::uuid
  AND splitter_version = :'expected_splitter'
  AND plan_sha256 = :'expected_plan_sha256';

SELECT 1 / ((count(*) = 1)::integer)
FROM memory.select_owner_contextual_generation_v1(
  :'target_evidence'::uuid,
  32
)
WHERE child_evidence_id = :'target_evidence'::uuid;

SELECT 1 / ((
  count(DISTINCT splitter_version) = 1
  AND count(DISTINCT plan_sha256) = 1
  AND count(DISTINCT parent_evidence_id) = 1
)::integer)
FROM memory.select_owner_contextual_generation_v1(
  :'target_evidence'::uuid,
  32
);

SELECT set_config('app.user_id', :'other_owner', true);
SELECT 1 / ((count(*) = 0)::integer)
FROM memory.select_owner_contextual_generation_v1(
  :'target_evidence'::uuid,
  32
);

DO $direct_table_access$
BEGIN
  PERFORM 1 FROM memory.evidence_contextual_span_v2 LIMIT 1;
  RAISE EXCEPTION 'brains_app unexpectedly read contextual span table';
EXCEPTION
  WHEN insufficient_privilege THEN NULL;
END
$direct_table_access$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_context_generation_selector_v1: PASS' AS result;
