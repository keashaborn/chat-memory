BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION
      'deferred-entailment reconciliation rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.claim_entailment_reconciliation_v5
  ) THEN
    RAISE EXCEPTION
      'deferred-entailment reconciliation rollback refuses live rows';
  END IF;
END
$guard$;

DROP FUNCTION memory.reconcile_deferred_entailment_claim_v5(
  uuid,uuid,uuid,uuid,uuid,text
);
DROP FUNCTION
  memory.preflight_deferred_entailment_reconciliation_v5(uuid,uuid);
DROP TABLE memory.claim_entailment_reconciliation_v5;
ALTER TABLE memory.observation_entailment_v5
  DROP CONSTRAINT observation_entailment_v5_owner_decision_key;

COMMIT;
