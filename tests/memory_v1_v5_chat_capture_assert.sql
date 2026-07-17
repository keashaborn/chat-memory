\set ON_ERROR_STOP on

BEGIN;

INSERT INTO memory.evidence_ingest_batch(
  batch_id,owner_user_id,batch_key,manifest_version,plan_version,
  input_fingerprint_sha256,reviewed_report_sha256,
  authorization_manifest_sha256,source_snapshot_sha256,
  source_row_count,expected_evidence_count,inserted_count,reused_count,
  actor_user_id,invoked_by_role,metadata
) VALUES (
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',
  'a1111111-1111-4111-8111-111111111111',
  'v5-chat-capture-fixture',
  'fixture_v1','fixture_v1',
  repeat('1',64),repeat('2',64),repeat('3',64),repeat('4',64),
  1,1,1,0,
  'a1111111-1111-4111-8111-111111111111',
  'brains_app',
  '{"fixture":true}'::jsonb
);

INSERT INTO memory.evidence_ingest_batch_row(
  owner_user_id,batch_id,evidence_id,external_id,content_sha256,operation
)
SELECT
  owner_user_id,
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',
  evidence_id,
  external_id,
  content_sha256,
  'inserted'
FROM memory.evidence
WHERE owner_user_id='a1111111-1111-4111-8111-111111111111'
  AND source_system='public.chat_log'
  AND external_id='11111111-1111-4111-8111-111111111102';

COMMIT;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','a1111111-1111-4111-8111-111111111111',false
);

SELECT 1 / ((count(*)=2)::integer)
FROM memory.evidence
WHERE source_system='public.chat_log'
  AND external_id IN (
    '11111111-1111-4111-8111-111111111101',
    '11111111-1111-4111-8111-111111111102'
  )
  AND status='active'
  AND metadata->>'capture_version'=
      'memory_v1_v5_chat_capture_20260717_v1';

SELECT 1 / ((count(*)=1)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260716_legacy_fixture',100,NULL
)
WHERE outcome='skipped'
  AND reason_code='upstream_review_required';

SELECT 1 / ((count(*)=2)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260717_v2_fixture',100,NULL
)
WHERE outcome='eligible'
  AND route='relational_extraction'
  AND reason_code='eligible_unprocessed';

SELECT set_config(
  'app.user_id','b2222222-2222-4222-8222-222222222222',false
);

SELECT 1 / ((count(*)=1)::integer)
FROM memory.evidence
WHERE source_system='public.chat_log'
  AND external_id='22222222-2222-4222-8222-222222222201';

SELECT 1 / ((count(*)=1)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260717_v2_fixture',100,NULL
)
WHERE outcome='eligible'
  AND route='relational_extraction';

SELECT 1 / ((count(*)=0)::integer)
FROM memory.evidence
WHERE external_id LIKE '11111111-1111-4111-8111-%';

RESET SESSION AUTHORIZATION;
