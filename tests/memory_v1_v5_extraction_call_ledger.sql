\set ON_ERROR_STOP on
BEGIN;

SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

SELECT *
FROM memory.claim_owner_v5_bounded_extraction_job_v1(
  '71111111-1111-4111-8111-111111111111',
  '71111111-1111-4111-8111-111111111112',
  'relational_extraction','call-ledger-test',300,1,
  'openai_responses','v1',repeat('a',64),86400,12,3
)
\gset claim_

SELECT
  1/((:'claim_status'='processing')::integer),
  1/((:'claim_control_outcome'='reserved')::integer),
  1/((:'claim_reserved_calls_in_window'='1')::integer),
  1/((:'claim_consecutive_rejections'='0')::integer),
  1/((:'claim_apply_outcome'='applied')::integer);

SELECT
  1/((reservation_event_id=:'claim_reservation_event_id')::integer),
  1/((job_id=:'claim_job_id')::integer),
  1/((apply_outcome='replayed')::integer)
FROM memory.claim_owner_v5_bounded_extraction_job_v1(
  '71111111-1111-4111-8111-111111111111',
  '71111111-1111-4111-8111-111111111112',
  'relational_extraction','call-ledger-test',300,1,
  'openai_responses','v1',repeat('a',64),86400,12,3
);

SELECT set_config(
  'test.claim_reservation_event_id',:'claim_reservation_event_id',true
);
SELECT set_config('test.claim_job_id',:'claim_job_id',true);

SELECT
  1/((job_id IS NULL)::integer),
  1/((control_outcome='quota_exhausted')::integer),
  1/((reserved_calls_in_window=1)::integer),
  1/((apply_outcome='applied')::integer)
FROM memory.claim_owner_v5_bounded_extraction_job_v1(
  '72222222-2222-4222-8222-222222222221',
  '72222222-2222-4222-8222-222222222222',
  'relational_extraction','call-ledger-test',300,1,
  'openai_responses','v1',repeat('a',64),86400,1,10
);

SELECT *
FROM memory.complete_owner_v5_extraction_call_v1(
  '73333333-3333-4333-8333-333333333331',
  :'claim_reservation_event_id',
  '71111111-1111-4111-8111-111111111112',
  :'claim_job_id','rejected',0,'synthetic_preflight_rejected',
  NULL,NULL,NULL
)
\gset completed_

SELECT
  1/((:'completed_outcome'='rejected')::integer),
  1/((:'completed_external_model_calls'='0')::integer),
  1/((:'completed_apply_outcome'='applied')::integer);

SELECT
  1/((event_id=:'completed_event_id')::integer),
  1/((apply_outcome='replayed')::integer)
FROM memory.complete_owner_v5_extraction_call_v1(
  '73333333-3333-4333-8333-333333333331',
  :'claim_reservation_event_id',
  '71111111-1111-4111-8111-111111111112',
  :'claim_job_id','rejected',0,'synthetic_preflight_rejected',
  NULL,NULL,NULL
);

SELECT
  1/((job_id IS NULL)::integer),
  1/((control_outcome='circuit_open')::integer),
  1/((consecutive_rejections=1)::integer),
  1/((apply_outcome='applied')::integer)
FROM memory.claim_owner_v5_bounded_extraction_job_v1(
  '74444444-4444-4444-8444-444444444441',
  '74444444-4444-4444-8444-444444444442',
  'relational_extraction','call-ledger-test',300,1,
  'openai_responses','v1',repeat('a',64),86400,100,1
);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $cross_owner$
BEGIN
  PERFORM * FROM memory.complete_owner_v5_extraction_call_v1(
    '75555555-5555-4555-8555-555555555551',
    current_setting('test.claim_reservation_event_id')::uuid,
    '71111111-1111-4111-8111-111111111112',
    current_setting('test.claim_job_id')::uuid,
    'rejected',0,'cross_owner_probe',NULL,NULL,NULL
  );
  RAISE EXCEPTION 'cross-owner completion unexpectedly succeeded';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$cross_owner$;

DO $direct_write$
BEGIN
  UPDATE memory.v5_extraction_call_event SET outcome='accepted';
  RAISE EXCEPTION 'direct call-ledger update unexpectedly succeeded';
EXCEPTION WHEN insufficient_privilege THEN
  NULL;
END
$direct_write$;

ROLLBACK;

SELECT 'memory_v1_v5_extraction_call_ledger: PASS' AS result;
