\set ON_ERROR_STOP on

BEGIN;

DO $security$
DECLARE
  planner regprocedure :=
    'memory.plan_owner_v5_local_packet_disposition_v1(integer)'::regprocedure;
  guard regprocedure :=
    'memory.guard_v5_legacy_packet_lane_v1()'::regprocedure;
  definition text;
BEGIN
  SELECT pg_get_functiondef(planner) INTO STRICT definition;
  IF position('memory_v1_relational_extraction_v5' IN definition)=0
     OR position('memory_predicate_registry_v5' IN definition)=0
     OR position('normalized_packet' IN definition)=0 THEN
    RAISE EXCEPTION 'legacy packet planner lacks exact contract gates';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_proc
    WHERE oid=planner AND prosecdef
      AND proowner='memory_v5_local_disposition_maintainer'::regrole
      AND EXISTS (
        SELECT 1 FROM unnest(proconfig) AS setting
        WHERE setting LIKE 'search_path=%'
      )
  ) OR NOT has_function_privilege('brains_app',planner,'EXECUTE')
     OR EXISTS (
       SELECT 1 FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid=planner AND acl.grantee=0
         AND acl.privilege_type='EXECUTE'
     ) THEN
    RAISE EXCEPTION 'legacy packet planner ownership or ACL is unsafe';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_proc
    WHERE oid=guard AND prosecdef
      AND proowner='memory_v5_local_disposition_maintainer'::regrole
      AND EXISTS (
        SELECT 1 FROM unnest(proconfig) AS setting
        WHERE setting LIKE 'search_path=%'
      )
  ) OR EXISTS (
    SELECT 1 FROM pg_proc AS procedure
    CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
    WHERE procedure.oid=guard AND acl.grantee=0
      AND acl.privilege_type='EXECUTE'
  ) THEN
    RAISE EXCEPTION 'legacy packet guard ownership or ACL is unsafe';
  END IF;

  IF (
    SELECT count(*) FROM pg_trigger
    WHERE tgname IN (
      'v5_local_disposition_contract_guard',
      'v5_local_review_artifact_contract_guard'
    ) AND tgrelid IN (
      'memory.v5_local_packet_disposition'::regclass,
      'memory.v5_local_packet_review_artifact'::regclass
    ) AND tgfoid=guard AND tgenabled='O'
  )<>2 THEN
    RAISE EXCEPTION 'legacy packet contract triggers are absent';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_disposition AS lane
    JOIN memory.evidence_extraction_packet_v5_local AS packet
      ON packet.owner_user_id=lane.owner_user_id
     AND packet.packet_id=lane.packet_id
    WHERE packet.normalized_packet->>'contract_version'
            IS DISTINCT FROM 'memory_v1_relational_extraction_v5'
       OR packet.normalized_packet->>'predicate_registry_version'
            IS DISTINCT FROM 'memory_predicate_registry_v5'
  ) OR EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_review_artifact AS lane
    JOIN memory.evidence_extraction_packet_v5_local AS packet
      ON packet.owner_user_id=lane.owner_user_id
     AND packet.packet_id=lane.packet_id
    WHERE packet.normalized_packet->>'contract_version'
            IS DISTINCT FROM 'memory_v1_relational_extraction_v5'
       OR packet.normalized_packet->>'predicate_registry_version'
            IS DISTINCT FROM 'memory_predicate_registry_v5'
  ) THEN
    RAISE EXCEPTION 'legacy packet lane contains a cross-profile row';
  END IF;
END
$security$;

ROLLBACK;

SELECT 'memory_v1_predicate_runtime_v5_1_downstream_isolation: PASS'
  AS result;
