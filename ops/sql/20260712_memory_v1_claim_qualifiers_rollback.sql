BEGIN;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM memory.claim) THEN
    RAISE EXCEPTION 'REFUSING rollback: memory.claim contains data';
  END IF;
END
$$;

ALTER TABLE memory.claim DROP COLUMN IF EXISTS qualifiers;

COMMIT;
