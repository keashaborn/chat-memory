BEGIN;

DO $$
DECLARE
  audit_rows_exist boolean := false;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 evidence ingest batch rollback must run as sage, current_user=%',
      current_user;
  END IF;

  IF to_regclass('memory.evidence_ingest_batch') IS NOT NULL THEN
    EXECUTE 'SELECT EXISTS (SELECT 1 FROM memory.evidence_ingest_batch LIMIT 1)'
      INTO audit_rows_exist;
    IF audit_rows_exist THEN
      RAISE EXCEPTION 'refusing to drop nonempty evidence ingest audit tables';
    END IF;
  END IF;
END
$$;

DO $$
BEGIN
  IF to_regclass('memory.evidence_ingest_batch_row') IS NOT NULL THEN
    DROP TRIGGER IF EXISTS evidence_ingest_batch_row_append_only_guard
      ON memory.evidence_ingest_batch_row;
  END IF;
  IF to_regclass('memory.evidence_ingest_batch') IS NOT NULL THEN
    DROP TRIGGER IF EXISTS evidence_ingest_batch_append_only_guard
      ON memory.evidence_ingest_batch;
  END IF;
END
$$;
DROP FUNCTION IF EXISTS memory.guard_evidence_ingest_audit_append_only();
DROP TABLE IF EXISTS memory.evidence_ingest_batch_row;
DROP TABLE IF EXISTS memory.evidence_ingest_batch;

COMMIT;
