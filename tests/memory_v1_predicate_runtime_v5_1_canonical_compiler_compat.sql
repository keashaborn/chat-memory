\set ON_ERROR_STOP on

BEGIN;

DO $security$
DECLARE
  target regprocedure :=
    'memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  recovery regprocedure :=
    'memory.requeue_owner_v5_1_persistence_mismatch_v1(uuid,uuid,text,uuid,uuid)'::regprocedure;
  expected_sha constant text :=
    'e79079a29210cf508dfc34571a9bb9501271d4ae3ba4990c1fb910f3f8877a06';
  canonical_compiler_sha constant text :=
    'af0e7b679480db10855cfb0ab2b705acd26a97238869e12b8b9a3f17bbc0024d';
  definition text;
  definition_sha text;
BEGIN
  SELECT pg_get_functiondef(target) INTO STRICT definition;
  definition_sha := encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');
  IF definition_sha<>expected_sha
     OR position(canonical_compiler_sha IN definition)=0
     OR position('memory_v1_relationship_policy_compiler_v14' IN definition)<>0 THEN
    RAISE EXCEPTION 'V5.1 canonical compiler persistence definition is invalid';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc
    WHERE oid=target AND prosecdef
      AND proowner='memory_v5_local_inference_maintainer'::regrole
      AND EXISTS (
        SELECT 1 FROM unnest(proconfig) AS setting
        WHERE setting LIKE 'search_path=%'
      )
  ) OR NOT has_function_privilege('brains_app',target,'EXECUTE')
     OR EXISTS (
       SELECT 1 FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid=target AND acl.grantee=0
         AND acl.privilege_type='EXECUTE'
     ) THEN
    RAISE EXCEPTION 'V5.1 canonical compiler persistence ACL is unsafe';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc
    WHERE oid=recovery AND prosecdef
      AND proowner='memory_v5_local_inference_maintainer'::regrole
      AND EXISTS (
        SELECT 1 FROM unnest(proconfig) AS setting
        WHERE setting LIKE 'search_path=%'
      )
  ) OR NOT has_function_privilege('brains_app',recovery,'EXECUTE')
     OR EXISTS (
       SELECT 1 FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid=recovery AND acl.grantee=0
         AND acl.privilege_type='EXECUTE'
     ) THEN
    RAISE EXCEPTION 'V5.1 persistence recovery ACL is unsafe';
  END IF;
END
$security$;

ROLLBACK;

SELECT 'memory_v1_predicate_runtime_v5_1_canonical_compiler_compat: PASS'
  AS result;
