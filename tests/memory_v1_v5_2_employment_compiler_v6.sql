\set ON_ERROR_STOP on

BEGIN;

DO $security$
DECLARE
  target constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  expected_sha constant text :=
    '39715a76bec7dceebbe1b15e25d2fff8300d42331a8bfb6c557070fa4f3e53ae';
  definition text;
  definition_sha text;
BEGIN
  SELECT pg_get_functiondef(target) INTO STRICT definition;
  definition_sha:=encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');
  IF definition_sha<>expected_sha
     OR position('memory_v1_semantic_policy_compiler_v6' IN definition)=0
     OR NOT EXISTS (
       SELECT 1 FROM pg_proc
       WHERE oid=target AND prosecdef
         AND proowner='memory_v5_local_inference_maintainer'::regrole
     )
     OR NOT has_function_privilege('brains_app',target,'EXECUTE')
     OR EXISTS (
       SELECT 1 FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid=target AND acl.grantee=0
         AND acl.privilege_type='EXECUTE'
     ) THEN
    RAISE EXCEPTION 'V5.2 compiler-v6 persistence boundary is invalid';
  END IF;
END
$security$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','cf111111-1111-4111-8111-111111111111',true
);

-- Compiler v6 must pass input validation and fail later on the absent lease.
DO $compiler_v6$
BEGIN
  PERFORM * FROM memory.persist_owner_v5_2_local_packet_v1(
    'cf800000-0000-4000-8000-000000000001',
    'cf800000-0000-4000-8000-000000000002',
    'cf400000-0000-4000-8000-000000000001',
    'cf800000-0000-4000-8000-000000000003','compiler-v6-test',
    repeat('a',64),'v1',repeat('3',64),repeat('4',64),repeat('5',64),
    '82a71a3ad7cdc729e83920bb02c8af79665340eb9b023503c538336e6a8dc6bd',
    repeat('8',64),repeat('9',64),
    '{"contract_version":"memory_v1_relational_extraction_v5_2","predicate_registry_version":"memory_predicate_registry_v5_2","entity_mentions":[],"observations":[],"comparison_hints":[],"deferrals":[],"packet_findings":[]}'::jsonb,
    false,1
  );
  RAISE EXCEPTION 'synthetic V5.2 compiler-v6 persistence unexpectedly succeeded';
EXCEPTION
  WHEN check_violation THEN NULL;
  WHEN invalid_parameter_value THEN
    RAISE EXCEPTION 'compiler v6 was rejected by persistence input validation';
END
$compiler_v6$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_2_employment_compiler_v6: PASS' AS result;
