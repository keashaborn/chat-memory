BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'deferred scan audit rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.deferred_reconciliation_scan_run_v5
  ) THEN
    RAISE EXCEPTION 'deferred scan audit rollback refuses live rows';
  END IF;
END
$guard$;

DROP FUNCTION memory.run_deferred_reconciliation_scan_v5(
  uuid,integer,text,text
);
DROP TABLE memory.deferred_reconciliation_scan_run_v5;

COMMIT;
