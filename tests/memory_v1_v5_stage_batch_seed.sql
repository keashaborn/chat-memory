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

INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,
  content,content_sha256,observed_at,directness,source_reliability,
  independence_key,sensitivity,status
) VALUES
  (
    'aeeeeeee-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'user_statement','public.chat_log',
    'aaaaaaaa-0001-4000-8000-000000000001',
    'What time is it?',
    'cd61bf0cc67fc2233111c91167e2abb3c652200bfe22059a166475d37366b7a8',
    '2026-07-16T12:00:00Z',1,1,
    'stage-batch-clone:owner-a-empty','low','active'
  ),
  (
    'aeeeeeee-1111-4111-8111-111111111112',
    '11111111-1111-4111-8111-111111111111',
    'user_statement','public.chat_log',
    'aaaaaaaa-0002-4000-8000-000000000002',
    'My name is Avery.',
    '3f55b194db3ae03b7100b9e8f572ab254f43bed87b151c3a5e0015163374d2aa',
    '2026-07-16T12:01:00Z',1,1,
    'stage-batch-clone:owner-a-name','low','active'
  ),
  (
    'beeeeeee-2222-4222-8222-222222222222',
    '22222222-2222-4222-8222-222222222222',
    'user_statement','public.chat_log',
    'bbbbbbbb-0001-4000-8000-000000000001',
    'Other owner evidence.',
    'e284bbab043edef01158fbe82bc74e6887109c27bd6dfbc3eed2fb645a40217a',
    '2026-07-16T12:02:00Z',1,1,
    'stage-batch-clone:owner-b','low','active'
  );

COMMIT;
