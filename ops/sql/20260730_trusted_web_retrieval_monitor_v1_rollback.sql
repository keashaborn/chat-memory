BEGIN;

DROP VIEW IF EXISTS trusted_web.retrieval_monitor_hourly_v1;
DROP INDEX IF EXISTS trusted_web.retrieval_audit_created_at_idx;

COMMIT;
