\set ON_ERROR_STOP on
BEGIN;

DO $precondition$
DECLARE
  auditor_oid oid;
BEGIN
  IF current_database() <> 'memory' OR current_user <> 'sage' THEN
    RAISE EXCEPTION 'legacy retirement auditor rollback target mismatch';
  END IF;
  SELECT oid INTO auditor_oid FROM pg_roles WHERE rolname = 'lifeswitch_retirement_auditor';
  IF auditor_oid IS NULL OR to_regprocedure('memory.legacy_retirement_evidence_v1()') IS NULL THEN
    RAISE EXCEPTION 'exact provisioned retirement boundary is not present';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_auth_members WHERE member = auditor_oid)
     OR NOT has_function_privilege('lifeswitch_retirement_auditor', 'memory.legacy_retirement_evidence_v1()', 'EXECUTE') THEN
    RAISE EXCEPTION 'retirement boundary drifted; rollback denied';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE usename = 'lifeswitch_retirement_auditor') THEN
    RAISE EXCEPTION 'retirement auditor has active sessions; rollback denied';
  END IF;
END;
$precondition$;

REVOKE EXECUTE ON FUNCTION memory.legacy_retirement_evidence_v1() FROM lifeswitch_retirement_auditor;
REVOKE USAGE ON SCHEMA memory FROM lifeswitch_retirement_auditor;
DROP FUNCTION memory.legacy_retirement_evidence_v1();
DROP ROLE lifeswitch_retirement_auditor;

COMMIT;
