\set ON_ERROR_STOP on

BEGIN;

DO $security$
DECLARE
  target constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  expected_sha constant text :=
    '4feb28bcf1a132a2c6f4b3628d11b3287db9c009090fd1a9ef68289c5af2f37e';
  definition text;
  definition_sha text;
BEGIN
  SELECT pg_get_functiondef(target) INTO STRICT definition;
  definition_sha:=encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');
  IF definition_sha<>expected_sha
     OR position('memory_v1_semantic_policy_compiler_v7' IN definition)=0
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
    RAISE EXCEPTION 'V5.2 compiler-v7 persistence boundary is invalid';
  END IF;
  IF to_regprocedure(
       'memory.requeue_owner_local_persistence_failure_v1(uuid,uuid,text,uuid,uuid,integer,text)'
     ) IS NULL
     OR NOT has_function_privilege(
       'brains_app',
       'memory.requeue_owner_local_persistence_failure_v1(uuid,uuid,text,uuid,uuid,integer,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'V5.2 compiler-v7 persistence retry boundary is invalid';
  END IF;
END
$security$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

-- Compiler v7 must pass input validation and fail later on the absent lease.
DO $compiler_v7$
BEGIN
  PERFORM * FROM memory.persist_owner_v5_2_local_packet_v1(
    'cf800000-0000-4000-8000-000000000001',
    'cf800000-0000-4000-8000-000000000002',
    'cf400000-0000-4000-8000-000000000001',
    'cf800000-0000-4000-8000-000000000003','compiler-v7-test',
    repeat('a',64),'v1',repeat('3',64),repeat('4',64),repeat('5',64),
    'a4387aad59445f65fdfc8413f48567b8f560a352cb6fcf8de415769b8752bfbc',
    repeat('8',64),repeat('9',64),
    '{"contract_version":"memory_v1_relational_extraction_v5_2","predicate_registry_version":"memory_predicate_registry_v5_2","entity_mentions":[],"observations":[],"comparison_hints":[],"deferrals":[],"packet_findings":[]}'::jsonb,
    false,0
  );
  RAISE EXCEPTION 'synthetic V5.2 compiler-v7 persistence unexpectedly succeeded';
EXCEPTION
  WHEN check_violation THEN NULL;
  WHEN invalid_parameter_value THEN
    RAISE EXCEPTION 'compiler v7 was rejected by persistence input validation';
END
$compiler_v7$;

DO $retry$
DECLARE
  applied record;
  replayed record;
BEGIN
  SELECT * INTO applied
  FROM memory.requeue_owner_local_persistence_failure_v1(
    'b7ab9077-331c-4502-91fb-7b9be6bac0a2',
    'db94d835-9598-5805-9101-f320e3c87f0f',
    'be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4',
    '0a03d151-d1a9-56cd-b319-e408292ce70e',
    '1bf5f805-63fb-4938-a0f2-d7e4480fc36a',
    1,'compiler_persistence_compatibility'
  );
  SELECT * INTO replayed
  FROM memory.requeue_owner_local_persistence_failure_v1(
    'b7ab9077-331c-4502-91fb-7b9be6bac0a2',
    'db94d835-9598-5805-9101-f320e3c87f0f',
    'be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4',
    '0a03d151-d1a9-56cd-b319-e408292ce70e',
    '1bf5f805-63fb-4938-a0f2-d7e4480fc36a',
    1,'compiler_persistence_compatibility'
  );
  IF applied.apply_outcome<>'applied' OR applied.status<>'pending'
     OR replayed.apply_outcome<>'replayed' THEN
    RAISE EXCEPTION 'V5.2 compiler-v7 persistence retry replay failed';
  END IF;
END
$retry$;

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $cross_owner$
BEGIN
  PERFORM * FROM memory.requeue_owner_local_persistence_failure_v1(
    '868e6429-abe6-42b7-a83a-1c1140c734f4',
    'db94d835-9598-5805-9101-f320e3c87f0f',
    'be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4',
    '0a03d151-d1a9-56cd-b319-e408292ce70e',
    '1bf5f805-63fb-4938-a0f2-d7e4480fc36a',
    1,'compiler_persistence_compatibility'
  );
  RAISE EXCEPTION 'cross-owner persistence retry unexpectedly passed';
EXCEPTION WHEN SQLSTATE '23514' THEN NULL;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_2_compiler_v7_persistence: PASS' AS result;
