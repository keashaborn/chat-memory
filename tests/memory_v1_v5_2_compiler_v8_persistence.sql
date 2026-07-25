\set ON_ERROR_STOP on

BEGIN;

DO $security$
DECLARE
  target constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  expected_sha constant text :=
    '4dcbd998364abc91d5f6abd0844546c74387f4c057fc876efbee7c222eb0c8ab';
  definition text;
BEGIN
  SELECT pg_get_functiondef(target) INTO STRICT definition;
  IF encode(public.digest(convert_to(
       definition,'UTF8'
     ),'sha256'),'hex')<>expected_sha
     OR position('memory_v1_semantic_policy_compiler_v8' IN definition)=0
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
    RAISE EXCEPTION 'V5.2 compiler-v8 persistence boundary is invalid';
  END IF;
END
$security$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

-- Compiler v8 must pass input validation and fail later on the absent lease.
DO $compiler_v8$
BEGIN
  PERFORM * FROM memory.persist_owner_v5_2_local_packet_v1(
    'cf900000-0000-4000-8000-000000000001',
    'cf900000-0000-4000-8000-000000000002',
    'cf400000-0000-4000-8000-000000000001',
    'cf900000-0000-4000-8000-000000000003','compiler-v8-test',
    repeat('a',64),'v1',repeat('3',64),repeat('4',64),repeat('5',64),
    'f82e6f4339dfe4aada7e5c3edb71fde8125a819f33677b3f47b98f3726b60419',
    repeat('8',64),repeat('9',64),
    '{"contract_version":"memory_v1_relational_extraction_v5_2","predicate_registry_version":"memory_predicate_registry_v5_2","entity_mentions":[],"observations":[],"comparison_hints":[],"deferrals":[],"packet_findings":[]}'::jsonb,
    false,0
  );
  RAISE EXCEPTION 'synthetic V5.2 compiler-v8 persistence unexpectedly succeeded';
EXCEPTION
  WHEN check_violation THEN NULL;
  WHEN invalid_parameter_value THEN
    RAISE EXCEPTION 'compiler v8 was rejected by persistence input validation';
END
$compiler_v8$;

-- An unregistered future compiler remains fail-closed.
DO $unknown_compiler$
BEGIN
  PERFORM * FROM memory.persist_owner_v5_2_local_packet_v1(
    'cfa00000-0000-4000-8000-000000000001',
    'cfa00000-0000-4000-8000-000000000002',
    'cf400000-0000-4000-8000-000000000001',
    'cfa00000-0000-4000-8000-000000000003','unknown-compiler-test',
    repeat('a',64),'v1',repeat('3',64),repeat('4',64),repeat('5',64),
    repeat('f',64),repeat('8',64),repeat('9',64),
    '{"contract_version":"memory_v1_relational_extraction_v5_2","predicate_registry_version":"memory_predicate_registry_v5_2","entity_mentions":[],"observations":[],"comparison_hints":[],"deferrals":[],"packet_findings":[]}'::jsonb,
    false,0
  );
  RAISE EXCEPTION 'unknown V5.2 compiler unexpectedly passed';
EXCEPTION
  WHEN invalid_parameter_value THEN NULL;
END
$unknown_compiler$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_2_compiler_v8_persistence: PASS' AS result;
