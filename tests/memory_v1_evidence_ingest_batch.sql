\set ON_ERROR_STOP on

DO $$
DECLARE
  unsafe text;
BEGIN
  SELECT string_agg(c.relname, ', ' ORDER BY c.relname)
  INTO unsafe
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'memory'
    AND c.relname IN ('evidence_ingest_batch', 'evidence_ingest_batch_row')
    AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity);

  IF unsafe IS NOT NULL THEN
    RAISE EXCEPTION 'evidence ingest audit tables lack forced RLS: %', unsafe;
  END IF;

  IF NOT has_table_privilege('brains_app', 'memory.evidence_ingest_batch', 'SELECT')
     OR NOT has_table_privilege('brains_app', 'memory.evidence_ingest_batch', 'INSERT')
     OR has_table_privilege('brains_app', 'memory.evidence_ingest_batch', 'UPDATE')
     OR has_table_privilege('brains_app', 'memory.evidence_ingest_batch', 'DELETE')
     OR NOT has_table_privilege('brains_app', 'memory.evidence_ingest_batch_row', 'SELECT')
     OR NOT has_table_privilege('brains_app', 'memory.evidence_ingest_batch_row', 'INSERT')
     OR has_table_privilege('brains_app', 'memory.evidence_ingest_batch_row', 'UPDATE')
     OR has_table_privilege('brains_app', 'memory.evidence_ingest_batch_row', 'DELETE') THEN
    RAISE EXCEPTION 'evidence ingest audit privileges are not insert-only';
  END IF;
END
$$;

BEGIN;
SET LOCAL ROLE brains_app;
SELECT set_config('app.user_id', '11111111-1111-4111-8111-111111111111', true);

INSERT INTO memory.evidence(
  evidence_id, owner_user_id, kind, source_system, external_id,
  content, content_sha256, directness, status
) VALUES (
  'b1000000-0000-4000-8000-000000000001',
  '11111111-1111-4111-8111-111111111111',
  'user_statement', 'public.chat_log', 'batch-a-span-1',
  'Actor A controlled batch fixture.', repeat('a', 64), 1.000, 'active'
);

INSERT INTO memory.evidence_ingest_batch(
  batch_id, owner_user_id, batch_key, manifest_version, plan_version,
  input_fingerprint_sha256, reviewed_report_sha256,
  authorization_manifest_sha256, source_snapshot_sha256,
  source_row_count, expected_evidence_count, inserted_count, reused_count,
  actor_user_id, invoked_by_role, metadata
) VALUES (
  'b2000000-0000-4000-8000-000000000001',
  '11111111-1111-4111-8111-111111111111',
  'memory-v1-evidence-ingest-test-a', 'test_manifest_v1', 'test_plan_v1',
  repeat('1', 64), repeat('2', 64), repeat('3', 64), repeat('4', 64),
  1, 1, 1, 0,
  '11111111-1111-4111-8111-111111111111', current_user,
  '{"test":true}'::jsonb
);

INSERT INTO memory.evidence_ingest_batch_row(
  owner_user_id, batch_id, evidence_id, external_id, content_sha256, operation
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'b2000000-0000-4000-8000-000000000001',
  'b1000000-0000-4000-8000-000000000001',
  'batch-a-span-1', repeat('a', 64), 'inserted'
);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    UPDATE memory.evidence_ingest_batch
    SET metadata = '{"changed":true}'::jsonb
    WHERE batch_id = 'b2000000-0000-4000-8000-000000000001';
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'brains_app updated an append-only batch';
  END IF;

  blocked := false;
  BEGIN
    DELETE FROM memory.evidence_ingest_batch_row
    WHERE batch_id = 'b2000000-0000-4000-8000-000000000001';
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'brains_app deleted an append-only batch row';
  END IF;
END
$$;

SELECT set_config('app.user_id', '22222222-2222-4222-8222-222222222222', true);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  IF (SELECT count(*) FROM memory.evidence_ingest_batch) <> 0
     OR (SELECT count(*) FROM memory.evidence_ingest_batch_row) <> 0 THEN
    RAISE EXCEPTION 'actor B can see actor A ingest audit rows';
  END IF;

  BEGIN
    INSERT INTO memory.evidence_ingest_batch(
      batch_id, owner_user_id, batch_key, manifest_version, plan_version,
      input_fingerprint_sha256, reviewed_report_sha256,
      authorization_manifest_sha256, source_snapshot_sha256,
      source_row_count, expected_evidence_count, inserted_count, reused_count,
      actor_user_id, invoked_by_role
    ) VALUES (
      'b2000000-0000-4000-8000-000000000002',
      '11111111-1111-4111-8111-111111111111',
      'cross-owner-batch', 'test_manifest_v1', 'test_plan_v1',
      repeat('1', 64), repeat('2', 64), repeat('3', 64), repeat('4', 64),
      1, 1, 1, 0,
      '11111111-1111-4111-8111-111111111111', current_user
    );
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'actor B inserted an actor A batch';
  END IF;
END
$$;

ROLLBACK;

BEGIN;
DO $$
DECLARE
  blocked boolean := false;
BEGIN
  INSERT INTO memory.evidence(
    evidence_id, owner_user_id, kind, source_system, external_id,
    content, content_sha256, directness, status
  ) VALUES (
    'b1000000-0000-4000-8000-000000000003',
    '11111111-1111-4111-8111-111111111111',
    'user_statement', 'public.chat_log', 'batch-owner-guard-span',
    'Table owner trigger fixture.', repeat('c', 64), 1.000, 'active'
  );
  INSERT INTO memory.evidence_ingest_batch(
    batch_id, owner_user_id, batch_key, manifest_version, plan_version,
    input_fingerprint_sha256, reviewed_report_sha256,
    authorization_manifest_sha256, source_snapshot_sha256,
    source_row_count, expected_evidence_count, inserted_count, reused_count,
    actor_user_id, invoked_by_role
  ) VALUES (
    'b2000000-0000-4000-8000-000000000003',
    '11111111-1111-4111-8111-111111111111',
    'owner-guard-batch', 'test_manifest_v1', 'test_plan_v1',
    repeat('1', 64), repeat('2', 64), repeat('3', 64), repeat('4', 64),
    1, 1, 1, 0,
    '11111111-1111-4111-8111-111111111111', 'brains_app'
  );
  BEGIN
    DELETE FROM memory.evidence_ingest_batch
    WHERE batch_id = 'b2000000-0000-4000-8000-000000000003';
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'table owner bypassed the append-only trigger';
  END IF;
END
$$;
ROLLBACK;

SELECT 'memory_v1_evidence_ingest_batch: PASS' AS result;
