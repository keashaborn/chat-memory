\set ON_ERROR_STOP on

BEGIN;

INSERT INTO public.chat_log(
  id,user_id,owner_user_id,source,text,created_at
) VALUES
  (
    '11111111-1111-4111-8111-111111111101',
    'a1111111-1111-4111-8111-111111111111',
    'a1111111-1111-4111-8111-111111111111',
    'frontend/chat:user',
    'Synthetic project decision: preserve technical design as project knowledge.',
    '2026-07-17T08:00:00Z'
  ),
  (
    '11111111-1111-4111-8111-111111111102',
    'a1111111-1111-4111-8111-111111111111',
    'a1111111-1111-4111-8111-111111111111',
    'frontend/chat:user',
    'Synthetic ordinary user statement.',
    '2026-07-17T08:01:00Z'
  ),
  (
    '11111111-1111-4111-8111-111111111103',
    'a1111111-1111-4111-8111-111111111111',
    'a1111111-1111-4111-8111-111111111111',
    'frontend/chat:assistant',
    'Synthetic assistant response that must not be captured.',
    '2026-07-17T08:02:00Z'
  ),
  (
    '22222222-2222-4222-8222-222222222201',
    'b2222222-2222-4222-8222-222222222222',
    'b2222222-2222-4222-8222-222222222222',
    'frontend/chat:user',
    'Synthetic second-owner statement.',
    '2026-07-17T08:03:00Z'
  );

INSERT INTO memory.consolidation_job(
  owner_user_id,source_system,source_external_id,source_sha256,
  source_recorded_at,status,pipeline_version,result
) VALUES (
  'a1111111-1111-4111-8111-111111111111',
  'public.chat_log',
  '11111111-1111-4111-8111-111111111101',
  encode(
    digest(
      convert_to(
        'Synthetic project decision: preserve technical design as project knowledge.',
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  ),
  '2026-07-17T08:00:00Z',
  'review_required',
  '20260714_v4',
  '{"route":"artifact_assessment"}'::jsonb
);

COMMIT;
