BEGIN;

DROP FUNCTION IF EXISTS memory.reconcile_correction_target_v5_2(
  uuid, uuid, uuid, uuid, text, text
);
DROP FUNCTION IF EXISTS memory.preflight_correction_target_reconciliation_v5_2(
  uuid, uuid, uuid, text
);
DROP TABLE IF EXISTS memory.entity_correction_target_reconciliation_v5_2;

COMMIT;
