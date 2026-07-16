\set ON_ERROR_STOP on

BEGIN;
SET SESSION AUTHORIZATION brains_app;
DO $missing_actor$
BEGIN
  BEGIN
    PERFORM * FROM memory.run_deferred_reconciliation_scan_v5(
      '46000000-0000-4000-8000-000000000001'::uuid,
      25,repeat('a',64),'memory_v1_deferred_scan_test'
    );
    RAISE EXCEPTION 'missing-actor scan run unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$missing_actor$;

SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
DO $direct_table_denial$
BEGIN
  BEGIN
    PERFORM 1 FROM memory.deferred_reconciliation_scan_run_v5 LIMIT 1;
    RAISE EXCEPTION 'brains_app directly read scan-run audit';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$direct_table_denial$;

SELECT * FROM memory.run_deferred_reconciliation_scan_v5(
  '46000000-0000-4000-8000-000000000001'::uuid,
  25,repeat('a',64),'memory_v1_deferred_scan_test'
) \gset owner_one_
SELECT 1 / ((:'owner_one_outcome'='applied')::integer);
SELECT 1 / ((:'owner_one_candidate_count'::integer=0)::integer);
SELECT 1 / ((:'owner_one_rows_written'::integer=1)::integer);

SELECT * FROM memory.run_deferred_reconciliation_scan_v5(
  '46000000-0000-4000-8000-000000000001'::uuid,
  25,repeat('a',64),'memory_v1_deferred_scan_test'
) \gset owner_one_replay_
SELECT 1 / ((:'owner_one_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'owner_one_replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'owner_one_replay_run_manifest_sha256'
  =:'owner_one_run_manifest_sha256')::integer);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT * FROM memory.run_deferred_reconciliation_scan_v5(
  '46000000-0000-4000-8000-000000000001'::uuid,
  25,repeat('a',64),'memory_v1_deferred_scan_test'
) \gset owner_two_
SELECT 1 / ((:'owner_two_outcome'='applied')::integer);
SELECT 1 / ((:'owner_two_candidate_count'::integer=0)::integer);
SELECT 1 / ((:'owner_two_rows_written'::integer=1)::integer);

RESET SESSION AUTHORIZATION;
SELECT 1 / (((SELECT count(*)
  FROM memory.deferred_reconciliation_scan_run_v5)=2)::integer);
SELECT 1 / (((
  SELECT count(DISTINCT owner_user_id)
  FROM memory.deferred_reconciliation_scan_run_v5
  WHERE run_id='46000000-0000-4000-8000-000000000001'::uuid
)=2)::integer);
ROLLBACK;
RESET SESSION AUTHORIZATION;

SELECT 1 / (((SELECT count(*)
  FROM memory.deferred_reconciliation_scan_run_v5)=0)::integer);
SELECT 'memory_v1_deferred_scan_audit_v5: PASS' AS result;
