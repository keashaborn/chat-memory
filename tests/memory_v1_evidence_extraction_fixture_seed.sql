\set ON_ERROR_STOP on

BEGIN;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','e1111111-1111-4111-8111-111111111111',true
);

SELECT evidence_id,content_sha256
FROM memory.record_owner_evidence_v1(
  'user_statement',
  'fixture_worker_test',
  'fixture-source-v1',
  'Synthetic fixture-only extraction source.',
  '2026-07-16T20:30:00Z',
  1,
  1,
  'fixture-source-v1',
  'low',
  '{"fixture_only":true}'::jsonb
)
\gset fixture_

SELECT 1/(
  (:'fixture_content_sha256'=
    'e7a9aa3849aee9aed7c5914fe1ff9140f7b30d5ee3e5b92e0553c821a436d29f'
  )::integer
);

SELECT 1/((apply_outcome='applied')::integer)
FROM memory.enqueue_owner_evidence_extraction_v1(
  :'fixture_evidence_id',
  '20260716_fixture_worker_v1',
  :'fixture_content_sha256',
  'relational_extraction',
  'eligible_unprocessed'
);

COMMIT;
