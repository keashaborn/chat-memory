\set ON_ERROR_STOP on
BEGIN;

DO $precondition$
DECLARE
  auditor_oid oid;
BEGIN
  IF current_database() <> 'memory' OR current_user <> 'sage' THEN
    RAISE EXCEPTION 'post-retirement auditor deprovision target mismatch';
  END IF;
  IF to_regnamespace('memory') IS NOT NULL OR to_regnamespace('memory_ingest_private') IS NOT NULL THEN
    RAISE EXCEPTION 'legacy schemas remain; post-retirement deprovision denied';
  END IF;
  SELECT oid INTO auditor_oid FROM pg_roles WHERE rolname = 'lifeswitch_retirement_auditor';
  IF auditor_oid IS NULL OR EXISTS (SELECT 1 FROM pg_auth_members WHERE member = auditor_oid) THEN
    RAISE EXCEPTION 'retirement auditor missing or drifted';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE usename = 'lifeswitch_retirement_auditor') THEN
    RAISE EXCEPTION 'retirement auditor has active sessions; deprovision denied';
  END IF;
END;
$precondition$;

DROP ROLE lifeswitch_retirement_auditor;
COMMIT;
