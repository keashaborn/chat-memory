
BEGIN;

DO $blocked$
BEGIN
  RAISE EXCEPTION USING
    ERRCODE = 'object_not_in_prerequisite_state',
    MESSAGE = 'legacy memory retirement blocked: run attestation reconciliation, encrypted quarantine, full backup, disposable restore, and external dependency proof first';
END;
$blocked$;

ROLLBACK;
