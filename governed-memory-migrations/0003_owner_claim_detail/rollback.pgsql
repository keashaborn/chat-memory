-- Data-preserving rollback for governed_memory_owner_claim_detail_0003.
-- Run only in governed_memory as governed_memory_owner. The migration runner
-- supplies the transaction, timeouts, and migration advisory lock.

DO $preflight$
DECLARE
  function_owner text;
BEGIN
  IF pg_catalog.current_database() <> 'governed_memory'
     OR current_user <> 'governed_memory_owner' THEN
    RAISE EXCEPTION
      'claim detail rollback requires governed_memory_owner in governed_memory';
  END IF;
  SELECT pg_catalog.pg_get_userbyid(procedure.proowner)
    INTO function_owner
  FROM pg_catalog.pg_proc AS procedure
  WHERE procedure.oid = pg_catalog.to_regprocedure(
    'memory_private.read_claim(uuid)'
  );
  IF function_owner IS DISTINCT FROM 'governed_memory_owner' THEN
    RAISE EXCEPTION 'claim detail function is absent or has unexpected owner';
  END IF;
END;
$preflight$;

DROP FUNCTION memory_private.read_claim(uuid);
