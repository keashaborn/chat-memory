BEGIN;

DO $test$
DECLARE
  function_text text;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM memory.predicate_registry_version
    WHERE registry_version = 'memory_predicate_registry_v5_2'
      AND contract_version = 'memory_v1_relational_extraction_v5_2'
      AND status = 'proposed'
      AND NOT runtime_active
      AND unknown_predicate_action = 'defer_unregistered_predicate'
      AND registry_sha256 = '446c0f9df2e3f12bab90cea5b056c5dc3da09a81c6d7ffd9c62252cdcf6c4876'
  ) THEN
    RAISE EXCEPTION 'V5.2 registry metadata mismatch';
  END IF;

  IF (SELECT count(*) FROM memory.predicate_contract
      WHERE registry_version = 'memory_predicate_registry_v5_2') <> 85
     OR (SELECT count(*) FROM memory.predicate_contract
         WHERE registry_version = 'memory_predicate_registry_v5_2'
           AND lifecycle = 'active' AND extraction_allowed) <> 66
     OR (SELECT count(*) FROM memory.predicate_contract
         WHERE registry_version = 'memory_predicate_registry_v5_2'
           AND lifecycle = 'legacy_read_only' AND NOT extraction_allowed) <> 19
     OR (SELECT count(*) FROM memory.relationship_predicate_contract_v5_2) <> 41
     OR (SELECT count(*) FROM memory.predicate_registry_source_binding_v5_2) <> 1 THEN
    RAISE EXCEPTION 'V5.2 registry component counts mismatch';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM memory.predicate_contract
    WHERE predicate = 'education.attended'
      AND registry_version = 'memory_predicate_registry_v5_2'
      AND lifecycle = 'active' AND extraction_allowed
      AND object_kind = 'entity' AND cardinality = 'many'
  ) OR NOT EXISTS (
    SELECT 1 FROM memory.predicate_contract
    WHERE predicate = 'employment.worked_for'
      AND registry_version = 'memory_predicate_registry_v5_2'
      AND lifecycle = 'active' AND extraction_allowed
      AND object_kind = 'entity' AND cardinality = 'many'
  ) OR NOT EXISTS (
    SELECT 1 FROM memory.predicate_contract
    WHERE predicate = 'stance.reported'
      AND registry_version = 'memory_predicate_registry_v5_2'
      AND lifecycle = 'active' AND extraction_allowed
      AND object_kind = 'literal' AND cardinality = 'many'
      AND contract->'modalities' = '["reported_belief","uncertain"]'::jsonb
      AND contract->'projection_classes' = '["reported_stance"]'::jsonb
  ) THEN
    RAISE EXCEPTION 'V5.2 semantic lane contracts mismatch';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory.relationship_predicate_contract_v5_2 AS relationship
    JOIN memory.predicate_contract AS contract
      ON contract.predicate = relationship.predicate
     AND contract.registry_version = relationship.registry_version
    WHERE contract.object_kind <> 'entity'
       OR contract.cardinality <> 'many'
       OR contract.lifecycle <> 'active'
       OR NOT contract.extraction_allowed
  ) THEN
    RAISE EXCEPTION 'V5.2 relationship storage contract mismatch';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM memory.predicate_registry_source_binding_v5_2
    WHERE source_name = 'canonical_registry'
      AND source_registry_version = 'memory_predicate_registry_v5_2'
      AND artifact_sha256 = 'e6ac5dfe7d7939aac23223ae76272b2e4f67777decde814d9bf0b8eee82b277e'
      AND canonical_sha256 = '446c0f9df2e3f12bab90cea5b056c5dc3da09a81c6d7ffd9c62252cdcf6c4876'
  ) THEN
    RAISE EXCEPTION 'V5.2 canonical source binding mismatch';
  END IF;

  IF EXISTS (
       SELECT 1
       FROM pg_class AS relation
       CROSS JOIN LATERAL aclexplode(
         coalesce(relation.relacl, acldefault('r', relation.relowner))
       ) AS acl
       WHERE relation.oid =
         'memory.relationship_predicate_contract_v5_2'::regclass
         AND acl.grantee = 0
         AND acl.privilege_type IN ('INSERT', 'UPDATE', 'DELETE')
     )
     OR has_table_privilege('memory_v5_writer',
       'memory.relationship_predicate_contract_v5_2', 'INSERT')
     OR has_table_privilege('memory_v5_writer',
       'memory.relationship_predicate_contract_v5_2', 'UPDATE')
     OR has_table_privilege('memory_v5_writer',
       'memory.relationship_predicate_contract_v5_2', 'DELETE') THEN
    RAISE EXCEPTION 'V5.2 relationship metadata has mutation privilege';
  END IF;

  SELECT pg_get_functiondef(
    'memory.stage_relational_packet_v5(uuid,uuid,text,text,text,text,text,text)'::regprocedure
  ) INTO function_text;
  IF strpos(function_text, 'memory_predicate_registry_v5_2') <> 0
     OR strpos(function_text, 'memory_predicate_registry_v5') = 0 THEN
    RAISE EXCEPTION 'current V5 writer is not pinned away from V5.2';
  END IF;

  IF EXISTS (
    SELECT 1 FROM memory.observation
    WHERE predicate_registry_version = 'memory_predicate_registry_v5_2'
  ) OR EXISTS (
    SELECT 1 FROM memory.entity_resolution_plan
    WHERE predicate_registry_version = 'memory_predicate_registry_v5_2'
  ) OR EXISTS (
    SELECT 1 FROM memory.projection_plan
    WHERE predicate_registry_version = 'memory_predicate_registry_v5_2'
  ) OR EXISTS (
    SELECT 1 FROM memory.projection_plan_item
    WHERE predicate_registry_version = 'memory_predicate_registry_v5_2'
  ) THEN
    RAISE EXCEPTION 'V5.2 registry unexpectedly has governed owner data';
  END IF;
END
$test$;

DO $immutability$
BEGIN
  BEGIN
    UPDATE memory.relationship_predicate_contract_v5_2
    SET family = family
    WHERE predicate = 'relationship.friend_of';
    RAISE EXCEPTION 'relationship metadata update unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '55000' THEN
    NULL;
  END;
  BEGIN
    DELETE FROM memory.predicate_registry_source_binding_v5_2
    WHERE source_name = 'canonical_registry';
    RAISE EXCEPTION 'source binding delete unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '55000' THEN
    NULL;
  END;
END
$immutability$;

ROLLBACK;
