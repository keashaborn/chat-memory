BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $guard$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.evidence_context_queue_reconciliation_v1
  ) THEN
    RAISE EXCEPTION
      'queue reconciliation history exists; rollback is intentionally blocked';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.finalize_owner_context_superseded_v1(
  uuid,uuid,uuid,uuid,uuid,uuid,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_local_orphan_v1(integer);
DROP FUNCTION IF EXISTS memory.plan_owner_context_superseded_v1(integer);
DROP TABLE IF EXISTS memory.evidence_context_queue_reconciliation_v1;

COMMIT;
