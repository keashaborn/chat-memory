\set ON_ERROR_STOP on

BEGIN;

INSERT INTO memory.entity(
  entity_id,owner_user_id,entity_key,entity_type,
  canonical_name,normalized_name,status
) VALUES
  (
    'a1111111-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'self','self','Owner A','owner a','active'
  ),
  (
    'b1111111-1111-4111-8111-111111111111',
    '22222222-2222-4222-8222-222222222222',
    'self','self','Owner B','owner b','active'
  );

INSERT INTO memory.project_space(
  project_id,owner_user_id,project_key,display_name,metadata
) VALUES
  (
    'a2000000-0000-4000-8000-000000000001',
    '11111111-1111-4111-8111-111111111111',
    'verbal-sage','Verbal Sage','{"test":true}'::jsonb
  ),
  (
    'b2000000-0000-4000-8000-000000000001',
    '22222222-2222-4222-8222-222222222222',
    'verbal-sage','Verbal Sage','{"test":true}'::jsonb
  );

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','11111111-1111-4111-8111-111111111111',true
);
SELECT 1 / ((component_key = 'memory-v1')::integer)
FROM memory.apply_owner_project_component_v5(
  'a2100000-0000-4000-8000-000000000001',
  'a2000000-0000-4000-8000-000000000001',
  'memory-v1','Memory V1',NULL,
  ARRAY['Memory','Memory system']::text[],
  '{"test":true}'::jsonb
);
SELECT set_config(
  'app.user_id','22222222-2222-4222-8222-222222222222',true
);
SELECT 1 / ((component_key = 'owner-b-only')::integer)
FROM memory.apply_owner_project_component_v5(
  'b2100000-0000-4000-8000-000000000001',
  'b2000000-0000-4000-8000-000000000001',
  'owner-b-only','Owner B Only',NULL,
  ARRAY['Private component']::text[],
  '{"test":true}'::jsonb
);
RESET SESSION AUTHORIZATION;

INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,
  content,content_sha256,observed_at,recorded_at,directness,source_reliability,
  independence_key,sensitivity,status
) VALUES
  (
    'aeeeeeee-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'user_statement','public.chat_log',
    'aaaaaaaa-0001-4000-8000-000000000001',
    'What time is it?',
    'cd61bf0cc67fc2233111c91167e2abb3c652200bfe22059a166475d37366b7a8',
    '2026-07-16T12:00:00Z','2026-07-16T12:00:00Z',1,1,
    'stage-batch-clone:owner-a-empty','low','active'
  ),
  (
    'aeeeeeee-1111-4111-8111-111111111112',
    '11111111-1111-4111-8111-111111111111',
    'user_statement','public.chat_log',
    'aaaaaaaa-0002-4000-8000-000000000002',
    'My name is Avery.',
    '3f55b194db3ae03b7100b9e8f572ab254f43bed87b151c3a5e0015163374d2aa',
    '2026-07-16T12:01:00Z','2026-07-16T12:01:00Z',1,1,
    'stage-batch-clone:owner-a-name','low','active'
  ),
  (
    'aeeeeeee-1111-4111-8111-111111111113',
    '11111111-1111-4111-8111-111111111111',
    'user_statement','public.chat_log',
    'aaaaaaaa-0003-4000-8000-000000000003',
    'Memory V1 must preserve component scope.',
    '0ba1eab04c0add2e8788319a0a93e67baec777332924f100103e6579c6fafcb6',
    '2026-07-16T12:02:00Z','2026-07-16T12:02:00Z',1,1,
    'stage-batch-clone:owner-a-component','medium','active'
  ),
  (
    'aeeeeeee-1111-4111-8111-111111111114',
    '11111111-1111-4111-8111-111111111111',
    'user_statement','public.chat_log',
    'aaaaaaaa-0004-4000-8000-000000000004',
    'Owner B Only must preserve component scope.',
    'b8ad4931c9875028655a6034eccc857fd2d6d990eb09621b587fca38b413359e',
    '2026-07-16T12:03:00Z','2026-07-16T12:03:00Z',1,1,
    'stage-batch-clone:owner-a-forged-component','medium','active'
  ),
  (
    'beeeeeee-2222-4222-8222-222222222222',
    '22222222-2222-4222-8222-222222222222',
    'user_statement','public.chat_log',
    'bbbbbbbb-0001-4000-8000-000000000001',
    'Other owner evidence.',
    'e284bbab043edef01158fbe82bc74e6887109c27bd6dfbc3eed2fb645a40217a',
    '2026-07-16T12:02:00Z','2026-07-16T12:02:00Z',1,1,
    'stage-batch-clone:owner-b','low','active'
  );

COMMIT;
