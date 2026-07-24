BEGIN;

DROP FUNCTION IF EXISTS memory.read_governed_entity_scope_edges_v1();
DROP FUNCTION IF EXISTS memory.read_governed_entity_scope_entities_v1();
REVOKE SELECT ON memory.entity_alias FROM memory_v5_reader;

COMMIT;
