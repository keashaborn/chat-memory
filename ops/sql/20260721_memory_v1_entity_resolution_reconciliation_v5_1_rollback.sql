BEGIN;

DROP FUNCTION IF EXISTS memory.apply_entity_resolution_v5_1(
  uuid, uuid, uuid, text
);
DROP FUNCTION IF EXISTS memory.preflight_entity_resolution_apply_v5_1(
  uuid, uuid
);
DROP FUNCTION IF EXISTS memory.review_entity_resolution_v5_1(
  uuid, uuid, memory.entity_review_decision, text, text
);
DROP FUNCTION IF EXISTS memory.preflight_entity_resolution_review_v5_1(
  uuid, memory.entity_review_decision, text
);
DROP FUNCTION IF EXISTS memory.reconcile_entity_resolution_v5_1(
  uuid, uuid, uuid, uuid, text, text
);
DROP FUNCTION IF EXISTS memory.preflight_entity_resolution_reconciliation_v5_1(
  uuid, uuid, uuid, text
);
DROP TABLE IF EXISTS memory.entity_resolution_reconciliation_v5_1;

COMMIT;
