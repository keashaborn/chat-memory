BEGIN;

DO $preflight$
DECLARE
  reader_owner text;
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 reader status-gate migration requires sage';
  END IF;
  IF to_regrole('memory_v5_reader') IS NULL
     OR to_regprocedure('memory.read_v5_shadow_claims(uuid[])') IS NULL
     OR to_regclass('memory.claim') IS NULL THEN
    RAISE EXCEPTION 'V5 reader status-gate prerequisites are absent';
  END IF;
  SELECT pg_get_userbyid(proowner) INTO reader_owner
  FROM pg_proc
  WHERE oid='memory.read_v5_shadow_claims(uuid[])'::regprocedure;
  IF reader_owner<>'memory_v5_reader'
     OR NOT (
       SELECT relrowsecurity AND relforcerowsecurity
       FROM pg_class WHERE oid='memory.claim'::regclass
     ) THEN
    RAISE EXCEPTION 'V5 shadow reader or claim RLS contract changed';
  END IF;
END
$preflight$;

DROP POLICY IF EXISTS surfaceable_status_v5_reader
  ON memory.claim;
CREATE POLICY surfaceable_status_v5_reader
  ON memory.claim
  AS RESTRICTIVE
  FOR SELECT
  TO memory_v5_reader
  USING (
    status IN ('candidate','supported','uncertain','disputed')
  );

COMMIT;
