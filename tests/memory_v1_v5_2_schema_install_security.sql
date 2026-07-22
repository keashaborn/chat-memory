\set ON_ERROR_STOP on

BEGIN;

DO $security$
DECLARE
  function_oid regprocedure;
  relation_oid regclass;
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM memory.predicate_registry_version
    WHERE registry_version='memory_predicate_registry_v5_2'
      AND contract_version='memory_v1_relational_extraction_v5_2'
      AND status='proposed'
      AND NOT runtime_active
  ) OR (SELECT count(*) FROM memory.predicate_contract
        WHERE registry_version='memory_predicate_registry_v5_2')<>85
    OR (SELECT count(*) FROM memory.predicate
        WHERE predicate IN (
          'education.attended','employment.worked_for','stance.reported'
        ))<>3
    OR (SELECT count(*) FROM memory.relationship_predicate_contract_v5_2)<>41
    OR (SELECT count(*) FROM memory.predicate_registry_source_binding_v5_2)<>1 THEN
    RAISE EXCEPTION 'V5.2 registry is incomplete or active';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_enum AS enum_value
    JOIN pg_type AS enum_type ON enum_type.oid=enum_value.enumtypid
    JOIN pg_namespace AS namespace ON namespace.oid=enum_type.typnamespace
    WHERE namespace.nspname='memory'
      AND enum_type.typname='observation_modality'
      AND enum_value.enumlabel='reported_belief'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_enum AS enum_value
    JOIN pg_type AS enum_type ON enum_type.oid=enum_value.enumtypid
    JOIN pg_namespace AS namespace ON namespace.oid=enum_type.typnamespace
    WHERE namespace.nspname='memory'
      AND enum_type.typname='observation_projection_class'
      AND enum_value.enumlabel='reported_stance'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_enum AS enum_value
    JOIN pg_type AS enum_type ON enum_type.oid=enum_value.enumtypid
    JOIN pg_namespace AS namespace ON namespace.oid=enum_type.typnamespace
    WHERE namespace.nspname='memory'
      AND enum_type.typname='observation_surface_policy'
      AND enum_value.enumlabel='relevant_recall_or_explicit_recall'
  ) THEN
    RAISE EXCEPTION 'V5.2 enum compatibility labels are absent';
  END IF;

  FOREACH relation_oid IN ARRAY ARRAY[
    'memory.entity_resolution_reconciliation_v5_2'::regclass,
    'memory.claim_relation_v5'::regclass,
    'memory.preference_relation_v5'::regclass,
    'memory.project_knowledge_relation_v5'::regclass,
    'memory.projection_dispatch_v5'::regclass
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_class
      WHERE oid=relation_oid AND relrowsecurity AND relforcerowsecurity
    ) OR has_table_privilege('brains_app',relation_oid,'INSERT')
      OR has_table_privilege('brains_app',relation_oid,'UPDATE')
      OR has_table_privilege('brains_app',relation_oid,'DELETE') THEN
      RAISE EXCEPTION 'V5.2 relation % has unsafe isolation or ACL',relation_oid;
    END IF;
  END LOOP;

  FOREACH function_oid IN ARRAY ARRAY[
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure,
    'memory.read_governed_claims_v2(uuid[])'::regprocedure,
    'memory.stage_relational_packet_v5_2(uuid,uuid,text,text,text,text,text,text)'::regprocedure,
    'memory.preflight_relational_stage_bundle_v5_2(uuid,text,text,timestamptz)'::regprocedure,
    'memory.preflight_entity_resolution_reconciliation_v5_2(uuid,uuid,uuid,text)'::regprocedure,
    'memory.reconcile_entity_resolution_v5_2(uuid,uuid,uuid,uuid,text,text)'::regprocedure,
    'memory.preflight_entity_resolution_review_v5_2(uuid,memory.entity_review_decision,text)'::regprocedure,
    'memory.review_entity_resolution_v5_2(uuid,uuid,memory.entity_review_decision,text,text)'::regprocedure,
    'memory.preflight_entity_resolution_apply_v5_2(uuid,uuid)'::regprocedure,
    'memory.apply_entity_resolution_v5_2(uuid,uuid,uuid,text)'::regprocedure,
    'memory.preflight_projection_source_v5_2(uuid)'::regprocedure,
    'memory.preflight_projection_packet_v5_2(uuid,text)'::regprocedure,
    'memory.stage_projection_plan_v5_2(uuid,text,text)'::regprocedure
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_proc
      WHERE oid=function_oid AND prosecdef
        AND proconfig IS NOT NULL
        AND EXISTS (
          SELECT 1 FROM unnest(proconfig) AS setting
          WHERE setting LIKE 'search_path=%'
        )
    ) OR NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
      OR EXISTS (
        SELECT 1 FROM pg_proc AS procedure
        CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
        WHERE procedure.oid=function_oid
          AND acl.grantee=0 AND acl.privilege_type='EXECUTE'
      ) THEN
      RAISE EXCEPTION 'V5.2 function % has unsafe ownership or ACL',function_oid;
    END IF;
  END LOOP;

  IF pg_get_userbyid((
       SELECT proowner FROM pg_proc
       WHERE oid='memory.read_governed_claims_v2(uuid[])'::regprocedure
     ))<>'memory_v5_reader'
     OR pg_get_userbyid((
       SELECT proowner FROM pg_proc
       WHERE oid='memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
     ))<>'memory_v5_local_inference_maintainer'
     OR EXISTS (
       SELECT 1 FROM pg_proc
       WHERE oid IN (
         'memory.stage_relational_packet_v5_2(uuid,uuid,text,text,text,text,text,text)'::regprocedure,
         'memory.preflight_entity_resolution_apply_v5_2(uuid,uuid)'::regprocedure,
         'memory.stage_projection_plan_v5_2(uuid,text,text)'::regprocedure
       ) AND proowner<>'memory_v5_writer'::regrole
     ) THEN
    RAISE EXCEPTION 'V5.2 function owner boundary is invalid';
  END IF;

  IF NOT has_table_privilege('memory_v5_reader','memory.entity','SELECT') THEN
    RAISE EXCEPTION 'governed V2 entity read boundary is invalid';
  END IF;

  IF EXISTS (
    SELECT 1 FROM memory.observation
    WHERE predicate_registry_version='memory_predicate_registry_v5_2'
  ) OR EXISTS (
    SELECT 1 FROM memory.entity_resolution_plan
    WHERE predicate_registry_version='memory_predicate_registry_v5_2'
  ) OR EXISTS (
    SELECT 1 FROM memory.projection_plan
    WHERE predicate_registry_version='memory_predicate_registry_v5_2'
  ) OR EXISTS (
    SELECT 1 FROM memory.entity_resolution_reconciliation_v5_2
  ) THEN
    RAISE EXCEPTION 'schema-only installation created governed V5.2 data';
  END IF;
END
$security$;

CREATE FUNCTION pg_temp.assert_missing_actor_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM * FROM memory.read_governed_claims_v2(ARRAY[]::uuid[]);
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'governed V2 reader accepted a missing actor';
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT pg_temp.assert_missing_actor_denied();
RESET SESSION AUTHORIZATION;

ROLLBACK;

SELECT 'memory_v1_v5_2_schema_install_security: PASS' AS result;
