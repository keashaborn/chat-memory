BEGIN;

DO $test$
DECLARE
  value_count integer;
  function_text text;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM memory.predicate_registry_version
    WHERE registry_version = 'memory_predicate_registry_v5_1'
      AND contract_version = 'memory_v1_relational_extraction_v5_1'
      AND status = 'proposed'
      AND NOT runtime_active
      AND unknown_predicate_action = 'defer_unregistered_predicate'
      AND registry_sha256 = '091fc66b325beba1e84f47916fe3e38cf170ab25f362c5b397c14daa793ec16e'
  ) THEN
    RAISE EXCEPTION 'V5.1 registry metadata mismatch';
  END IF;

  SELECT count(*) INTO value_count
  FROM memory.predicate_contract
  WHERE registry_version = 'memory_predicate_registry_v5_1';
  IF value_count <> 82 THEN
    RAISE EXCEPTION 'expected 82 V5.1 predicate contracts, got %', value_count;
  END IF;

  IF (SELECT count(*) FROM memory.predicate_contract
      WHERE registry_version = 'memory_predicate_registry_v5_1'
        AND lifecycle = 'active' AND extraction_allowed) <> 63
     OR (SELECT count(*) FROM memory.predicate_contract
         WHERE registry_version = 'memory_predicate_registry_v5_1'
           AND lifecycle = 'legacy_read_only' AND NOT extraction_allowed) <> 19
     OR (SELECT count(*) FROM memory.relationship_predicate_contract_v5_1) <> 41
     OR (SELECT count(*) FROM memory.predicate_registry_source_binding_v5_1) <> 2 THEN
    RAISE EXCEPTION 'V5.1 registry component counts mismatch';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory.relationship_predicate_contract_v5_1 AS relationship
    JOIN memory.predicate_contract AS contract
      ON contract.predicate = relationship.predicate
     AND contract.registry_version = relationship.registry_version
    WHERE contract.object_kind <> 'entity'
       OR contract.cardinality <> 'many'
       OR contract.lifecycle <> 'active'
       OR NOT contract.extraction_allowed
  ) THEN
    RAISE EXCEPTION 'relationship storage contract mismatch';
  END IF;

  IF EXISTS (
    SELECT 1 FROM memory.relationship_predicate_contract_v5_1
    WHERE family = 'relational_state'
      AND (
        perspective <> 'owner_reported_state'
        OR temporal_profile <> 'dynamic_state'
        OR NOT (contract->'subject_entity_types' @> '["self"]'::jsonb)
      )
  ) THEN
    RAISE EXCEPTION 'relational-state policy mismatch';
  END IF;

  IF EXISTS (
    SELECT 1 FROM memory.predicate_contract
    WHERE registry_version = 'memory_predicate_registry_v5_1'
      AND predicate IN ('relationship.enemy_of', 'social.enemy_of')
  ) OR NOT EXISTS (
    SELECT 1 FROM memory.relationship_predicate_contract_v5_1
    WHERE predicate = 'social.perceives_as_adversary'
      AND sensitivity_floor = 'restricted'
      AND observation_surface_policy = 'explicit_recall_only'
  ) THEN
    RAISE EXCEPTION 'adversarial-state fail-closed policy mismatch';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM memory.relationship_predicate_contract_v5_1
    WHERE predicate = 'relationship.parent_of'
      AND canonical_direction = 'parent_to_child'
      AND relation_semantics = 'directed'
  ) OR NOT EXISTS (
    SELECT 1 FROM memory.relationship_predicate_contract_v5_1
    WHERE predicate = 'relationship.sibling_of'
      AND canonical_direction = 'unordered_entity_pair'
      AND relation_semantics = 'symmetric'
  ) THEN
    RAISE EXCEPTION 'canonical relationship direction mismatch';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM memory.predicate_registry_source_binding_v5_1
    WHERE source_name = 'base_registry'
      AND artifact_sha256 = '4837cc66f8ef41d5b091528c02e06add267586cb170dc0eb4b57fc207bd0f3d8'
      AND canonical_sha256 = 'c7d6397c804699ae3afa4538fe6c9535b5b1e08c357ae3a18105b81035f5cacc'
  ) OR NOT EXISTS (
    SELECT 1 FROM memory.predicate_registry_source_binding_v5_1
    WHERE source_name = 'relationship_registry'
      AND artifact_sha256 = '01d0045cf5607b55eb7d2b97611c5810b81c6098c6118435a2015d5cc8102a4f'
      AND canonical_sha256 = '06d191649014fc0d191dc07d6f5b05c58c266c343649744f9572e68d9d381a5c'
  ) THEN
    RAISE EXCEPTION 'V5.1 source bindings mismatch';
  END IF;

  IF EXISTS (
       SELECT 1
       FROM pg_class AS relation
       CROSS JOIN LATERAL aclexplode(
         coalesce(relation.relacl, acldefault('r', relation.relowner))
       ) AS acl
       WHERE relation.oid =
         'memory.relationship_predicate_contract_v5_1'::regclass
         AND acl.grantee = 0
         AND acl.privilege_type IN ('INSERT', 'UPDATE', 'DELETE')
     )
     OR has_table_privilege('memory_v5_writer',
       'memory.relationship_predicate_contract_v5_1', 'INSERT')
     OR has_table_privilege('memory_v5_writer',
       'memory.relationship_predicate_contract_v5_1', 'UPDATE')
     OR has_table_privilege('memory_v5_writer',
       'memory.relationship_predicate_contract_v5_1', 'DELETE') THEN
    RAISE EXCEPTION 'V5.1 relationship metadata has mutation privilege';
  END IF;

  SELECT pg_get_functiondef(
    'memory.stage_relational_packet_v5(uuid,uuid,text,text,text,text,text,text)'::regprocedure
  ) INTO function_text;
  IF strpos(function_text, 'memory_predicate_registry_v5_1') <> 0
     OR strpos(function_text, 'memory_predicate_registry_v5') = 0 THEN
    RAISE EXCEPTION 'current V5 writer is not pinned away from V5.1';
  END IF;

  IF EXISTS (
    SELECT 1 FROM memory.observation
    WHERE predicate_registry_version = 'memory_predicate_registry_v5_1'
  ) OR EXISTS (
    SELECT 1 FROM memory.entity_resolution_plan
    WHERE predicate_registry_version = 'memory_predicate_registry_v5_1'
  ) THEN
    RAISE EXCEPTION 'V5.1 registry unexpectedly has live owner data';
  END IF;
END
$test$;

DO $immutability$
BEGIN
  BEGIN
    UPDATE memory.relationship_predicate_contract_v5_1
    SET family = family
    WHERE predicate = 'relationship.friend_of';
    RAISE EXCEPTION 'relationship metadata update unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '55000' THEN
    NULL;
  END;
  BEGIN
    DELETE FROM memory.predicate_registry_source_binding_v5_1
    WHERE source_name = 'base_registry';
    RAISE EXCEPTION 'source binding delete unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '55000' THEN
    NULL;
  END;
END
$immutability$;

ROLLBACK;
