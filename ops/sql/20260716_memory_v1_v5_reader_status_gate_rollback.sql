BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 reader status-gate rollback requires sage';
  END IF;
END
$guard$;

DROP POLICY IF EXISTS surfaceable_status_v5_reader
  ON memory.claim;

COMMIT;
