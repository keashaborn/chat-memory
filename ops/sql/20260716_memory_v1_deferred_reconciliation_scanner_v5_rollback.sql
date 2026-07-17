BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'deferred reconciliation scanner rollback requires sage';
  END IF;
END
$guard$;

DROP FUNCTION memory.scan_deferred_entailment_reconciliation_v5(integer);
DROP FUNCTION memory.v5_deferred_support_state_eligible(jsonb);

COMMIT;
