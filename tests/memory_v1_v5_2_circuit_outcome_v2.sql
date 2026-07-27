\set ON_ERROR_STOP on

BEGIN;

-- All fixtures and assertions are rolled back. No model or network call occurs.
SELECT
  count(*) AS claims,
  (SELECT count(*) FROM memory.observation) AS observations,
  (SELECT count(*) FROM memory.entity) AS entities,
  (SELECT count(*) FROM memory.projection_outbox) AS projections,
  (SELECT count(*) FROM memory.final_answer_memory_binding_v1) AS bindings,
  (SELECT count(*) FROM memory.evidence_extraction_packet_v5_local) AS packets
FROM memory.claim
\gset before_

INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,content,
  content_sha256,recorded_at,sensitivity,status,metadata
) VALUES
('ca200000-0000-4000-8000-000000000001',
 'ca111111-1111-4111-8111-111111111111','user_statement',
 'public.chat_log','outcome-v2-history','Synthetic history.',repeat('a',64),
 clock_timestamp(),'medium','active','{}'),
('ca200000-0000-4000-8000-000000000002',
 'ca111111-1111-4111-8111-111111111111','user_statement',
 'public.chat_log','outcome-v2-semantic','Synthetic semantic target.',repeat('b',64),
 clock_timestamp(),'medium','active','{}'),
('ca200000-0000-4000-8000-000000000003',
 'ca111111-1111-4111-8111-111111111111','user_statement',
 'public.chat_log','outcome-v2-systemic','Synthetic systemic target.',repeat('c',64),
 clock_timestamp(),'medium','active','{}'),
('ca200000-0000-4000-8000-000000000004',
 'ca111111-1111-4111-8111-111111111111','user_statement',
 'public.chat_log','outcome-v2-half-one','Synthetic half-open target one.',
 repeat('d',64),clock_timestamp(),'medium','active','{}'),
('ca200000-0000-4000-8000-000000000005',
 'ca111111-1111-4111-8111-111111111111','user_statement',
 'public.chat_log','outcome-v2-half-two','Synthetic half-open target two.',
 repeat('e',64),clock_timestamp(),'medium','active','{}'),
('ca200000-0000-4000-8000-000000000006',
 'ca111111-1111-4111-8111-111111111111','user_statement',
 'public.chat_log','outcome-v2-review','Synthetic review target.',repeat('f',64),
 clock_timestamp(),'high','active','{}'),
('ca200000-0000-4000-8000-000000000007',
 'ca111111-1111-4111-8111-111111111111','user_statement',
 'public.chat_log','outcome-v2-deferred','Synthetic deferred target.',
 repeat('0',64),clock_timestamp(),'medium','active','{}'),
('cb200000-0000-4000-8000-000000000001',
 'cb111111-1111-4111-8111-111111111111','user_statement',
 'public.chat_log','outcome-v2-other-owner','Synthetic other owner target.',
 repeat('9',64),clock_timestamp(),'medium','active','{}');

INSERT INTO memory.evidence_intake_terminal(
  terminal_id,owner_user_id,evidence_id,selector_version,outcome,reason_code,
  evidence_content_sha256,decision_fingerprint,actor_user_id,
  invoked_by_role,details
)
SELECT
  terminal_id,owner_user_id,evidence_id,'outcome_v2_test','dispatched',
  'eligible_dispatched',content_sha,repeat(fingerprint_digit,64),
  owner_user_id,'sage','{"route":"relational_extraction"}'::jsonb
FROM (VALUES
  ('ca300000-0000-4000-8000-000000000001'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000001'::uuid,repeat('a',64),'1'),
  ('ca300000-0000-4000-8000-000000000002'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000002'::uuid,repeat('b',64),'2'),
  ('ca300000-0000-4000-8000-000000000003'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000003'::uuid,repeat('c',64),'3'),
  ('ca300000-0000-4000-8000-000000000004'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000004'::uuid,repeat('d',64),'4'),
  ('ca300000-0000-4000-8000-000000000005'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000005'::uuid,repeat('e',64),'5'),
  ('ca300000-0000-4000-8000-000000000006'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000006'::uuid,repeat('f',64),'6'),
  ('ca300000-0000-4000-8000-000000000007'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000007'::uuid,repeat('0',64),'7'),
  ('cb300000-0000-4000-8000-000000000001'::uuid,
   'cb111111-1111-4111-8111-111111111111'::uuid,
   'cb200000-0000-4000-8000-000000000001'::uuid,repeat('9',64),'8')
) AS fixture(
  terminal_id,owner_user_id,evidence_id,content_sha,fingerprint_digit
);

INSERT INTO memory.evidence_extraction_job(
  job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
  evidence_content_sha256,route,intake_reason_code,status,priority
)
SELECT
  job_id,owner_user_id,evidence_id,terminal_id,'outcome_v2_test',
  content_sha,'relational_extraction','eligible_unprocessed','pending',10
FROM (VALUES
  ('ca400000-0000-4000-8000-000000000001'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000001'::uuid,
   'ca300000-0000-4000-8000-000000000001'::uuid,repeat('a',64)),
  ('ca400000-0000-4000-8000-000000000002'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000002'::uuid,
   'ca300000-0000-4000-8000-000000000002'::uuid,repeat('b',64)),
  ('ca400000-0000-4000-8000-000000000003'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000003'::uuid,
   'ca300000-0000-4000-8000-000000000003'::uuid,repeat('c',64)),
  ('ca400000-0000-4000-8000-000000000004'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000004'::uuid,
   'ca300000-0000-4000-8000-000000000004'::uuid,repeat('d',64)),
  ('ca400000-0000-4000-8000-000000000005'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000005'::uuid,
   'ca300000-0000-4000-8000-000000000005'::uuid,repeat('e',64)),
  ('ca400000-0000-4000-8000-000000000006'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000006'::uuid,
   'ca300000-0000-4000-8000-000000000006'::uuid,repeat('f',64)),
  ('ca400000-0000-4000-8000-000000000007'::uuid,
   'ca111111-1111-4111-8111-111111111111'::uuid,
   'ca200000-0000-4000-8000-000000000007'::uuid,
   'ca300000-0000-4000-8000-000000000007'::uuid,repeat('0',64)),
  ('cb400000-0000-4000-8000-000000000001'::uuid,
   'cb111111-1111-4111-8111-111111111111'::uuid,
   'cb200000-0000-4000-8000-000000000001'::uuid,
   'cb300000-0000-4000-8000-000000000001'::uuid,repeat('9',64))
) AS fixture(job_id,owner_user_id,evidence_id,terminal_id,content_sha);

INSERT INTO memory.evidence_extraction_event(
  owner_user_id,job_id,event_type,from_status,to_status,actor_type,
  actor_ref,details
)
SELECT owner_user_id,job_id,'queued',NULL,'pending','system',
  'outcome-v2-test','{}'::jsonb
FROM memory.evidence_extraction_job
WHERE selector_version='outcome_v2_test';

-- Ten ordinary record-level outcomes under one exact runtime fingerprint.
DO $semantic$
DECLARE
  i integer;
  reservation_id uuid;
  completed_at timestamptz;
BEGIN
  FOR i IN 1..10 LOOP
    reservation_id:=gen_random_uuid();
    completed_at:=clock_timestamp()-interval '30 seconds'
      +make_interval(secs=>i::double precision/1000);
    INSERT INTO memory.v5_local_inference_event(
      event_id,owner_user_id,operation_id,run_id,job_id,target_job_sha256,
      target_content_sha256,reservation_event_id,action,outcome,provider_id,
      provider_version,provider_model_sha256,model_file_sha256,
      runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
      local_model_calls,external_model_calls,rejection_code,
      rolling_window_seconds,max_reserved_jobs,failure_threshold,
      reserved_jobs_in_window,consecutive_rejections,created_at
    ) VALUES (
      reservation_id,'ca111111-1111-4111-8111-111111111111',
      gen_random_uuid(),gen_random_uuid(),
      'ca400000-0000-4000-8000-000000000001',repeat('a',64),repeat('a',64),
      NULL,'reserved','reserved','local_llama_cpp','outcome-v2',
      repeat('1',64),repeat('2',64),repeat('3',64),repeat('4',64),
      repeat('5',64),0,0,NULL,3600,100,10,i,i-1,
      completed_at-interval '1 millisecond'
    );
    INSERT INTO memory.v5_local_inference_event(
      event_id,owner_user_id,operation_id,run_id,job_id,target_job_sha256,
      target_content_sha256,reservation_event_id,action,outcome,provider_id,
      provider_version,provider_model_sha256,model_file_sha256,
      runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
      local_model_calls,external_model_calls,rejection_code,
      rolling_window_seconds,max_reserved_jobs,failure_threshold,
      reserved_jobs_in_window,consecutive_rejections,created_at
    ) VALUES (
      gen_random_uuid(),'ca111111-1111-4111-8111-111111111111',
      gen_random_uuid(),gen_random_uuid(),
      'ca400000-0000-4000-8000-000000000001',repeat('a',64),repeat('a',64),
      reservation_id,'completed','rejected','local_llama_cpp','outcome-v2',
      repeat('1',64),repeat('2',64),repeat('3',64),repeat('4',64),
      repeat('5',64),1,0,'context_coreference_unresolved',
      3600,100,10,i,i,completed_at
    );
  END LOOP;
END
$semantic$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','ca111111-1111-4111-8111-111111111111',true);

SELECT * FROM memory.claim_owner_v5_local_inference_job_v1(
  'ca900000-0000-4000-8000-000000000001',
  'ca900000-0000-4000-8000-000000000002',
  'ca400000-0000-4000-8000-000000000002',repeat('b',64),
  'relational_extraction','outcome-v2-test',900,2,
  'local_llama_cpp','outcome-v2',repeat('1',64),repeat('2',64),
  repeat('3',64),repeat('4',64),3600,100,10
) \gset semantic_
SELECT
  1/((:'semantic_control_outcome'='reserved')::integer),
  1/((:'semantic_consecutive_rejections'='0')::integer),
  1/((:'semantic_job_id'='ca400000-0000-4000-8000-000000000002')::integer);

RESET SESSION AUTHORIZATION;

-- Ten systemic transport failures under a separate exact fingerprint.
DO $systemic$
DECLARE
  i integer;
  reservation_id uuid;
BEGIN
  FOR i IN 1..10 LOOP
    reservation_id:=gen_random_uuid();
    INSERT INTO memory.v5_local_inference_event(
      event_id,owner_user_id,operation_id,run_id,job_id,target_job_sha256,
      target_content_sha256,reservation_event_id,action,outcome,provider_id,
      provider_version,provider_model_sha256,model_file_sha256,
      runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
      local_model_calls,external_model_calls,rejection_code,
      rolling_window_seconds,max_reserved_jobs,failure_threshold,
      reserved_jobs_in_window,consecutive_rejections,created_at
    ) VALUES (
      reservation_id,'ca111111-1111-4111-8111-111111111111',
      gen_random_uuid(),gen_random_uuid(),
      'ca400000-0000-4000-8000-000000000001',repeat('a',64),repeat('a',64),
      NULL,'reserved','reserved','local_llama_cpp','outcome-v2',
      repeat('1',64),repeat('2',64),repeat('3',64),repeat('6',64),
      repeat('5',64),0,0,NULL,3600,100,10,i,i-1,
      clock_timestamp()-interval '20 seconds'
        +make_interval(secs=>i::double precision/1000)
    );
    INSERT INTO memory.v5_local_inference_event(
      event_id,owner_user_id,operation_id,run_id,job_id,target_job_sha256,
      target_content_sha256,reservation_event_id,action,outcome,provider_id,
      provider_version,provider_model_sha256,model_file_sha256,
      runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
      local_model_calls,external_model_calls,rejection_code,
      rolling_window_seconds,max_reserved_jobs,failure_threshold,
      reserved_jobs_in_window,consecutive_rejections,created_at
    ) VALUES (
      gen_random_uuid(),'ca111111-1111-4111-8111-111111111111',
      gen_random_uuid(),gen_random_uuid(),
      'ca400000-0000-4000-8000-000000000001',repeat('a',64),repeat('a',64),
      reservation_id,'completed','rejected','local_llama_cpp','outcome-v2',
      repeat('1',64),repeat('2',64),repeat('3',64),repeat('6',64),
      repeat('5',64),0,0,'local_transport_timeout',
      3600,100,10,i,i,
      clock_timestamp()-interval '20 seconds'
        +make_interval(secs=>i::double precision/1000)
    );
  END LOOP;
END
$systemic$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','ca111111-1111-4111-8111-111111111111',true);
SELECT * FROM memory.claim_owner_v5_local_inference_job_v1(
  'ca910000-0000-4000-8000-000000000001',
  'ca910000-0000-4000-8000-000000000002',
  'ca400000-0000-4000-8000-000000000003',repeat('c',64),
  'relational_extraction','outcome-v2-test',900,2,
  'local_llama_cpp','outcome-v2',repeat('1',64),repeat('2',64),
  repeat('3',64),repeat('6',64),3600,100,10
) \gset systemic_
SELECT
  1/((:'systemic_control_outcome'='circuit_open')::integer),
  1/((:'systemic_consecutive_rejections'='10')::integer);
RESET SESSION AUTHORIZATION;
SELECT 1/((count(*)=1)::integer)
FROM memory.v5_local_inference_event
WHERE owner_user_id='ca111111-1111-4111-8111-111111111111'
  AND operation_id='ca910000-0000-4000-8000-000000000001'
  AND action='blocked' AND outcome='circuit_open' AND local_model_calls=0;
SELECT 1/((count(*)=0)::integer)
FROM memory.evidence_extraction_event
WHERE owner_user_id='ca111111-1111-4111-8111-111111111111'
  AND job_id='ca400000-0000-4000-8000-000000000003'
  AND event_type='claimed';
SELECT 1/((attempts=0 AND status='pending')::integer)
FROM memory.evidence_extraction_job
WHERE owner_user_id='ca111111-1111-4111-8111-111111111111'
  AND job_id='ca400000-0000-4000-8000-000000000003';

-- A cooled-down circuit allows exactly one half-open reservation.
DO $half_open$
DECLARE
  i integer;
  reservation_id uuid;
BEGIN
  FOR i IN 1..10 LOOP
    reservation_id:=gen_random_uuid();
    INSERT INTO memory.v5_local_inference_event(
      event_id,owner_user_id,operation_id,run_id,job_id,target_job_sha256,
      target_content_sha256,reservation_event_id,action,outcome,provider_id,
      provider_version,provider_model_sha256,model_file_sha256,
      runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
      local_model_calls,external_model_calls,rejection_code,
      rolling_window_seconds,max_reserved_jobs,failure_threshold,
      reserved_jobs_in_window,consecutive_rejections,created_at
    ) VALUES (
      reservation_id,'ca111111-1111-4111-8111-111111111111',
      gen_random_uuid(),gen_random_uuid(),
      'ca400000-0000-4000-8000-000000000001',repeat('a',64),repeat('a',64),
      NULL,'reserved','reserved','local_llama_cpp','outcome-v2',
      repeat('1',64),repeat('2',64),repeat('3',64),repeat('7',64),
      repeat('5',64),0,0,NULL,3600,100,10,i,i-1,
      clock_timestamp()-interval '20 minutes'
        +make_interval(secs=>i::double precision/1000)
    );
    INSERT INTO memory.v5_local_inference_event(
      event_id,owner_user_id,operation_id,run_id,job_id,target_job_sha256,
      target_content_sha256,reservation_event_id,action,outcome,provider_id,
      provider_version,provider_model_sha256,model_file_sha256,
      runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
      local_model_calls,external_model_calls,rejection_code,
      rolling_window_seconds,max_reserved_jobs,failure_threshold,
      reserved_jobs_in_window,consecutive_rejections,created_at
    ) VALUES (
      gen_random_uuid(),'ca111111-1111-4111-8111-111111111111',
      gen_random_uuid(),gen_random_uuid(),
      'ca400000-0000-4000-8000-000000000001',repeat('a',64),repeat('a',64),
      reservation_id,'completed','rejected','local_llama_cpp','outcome-v2',
      repeat('1',64),repeat('2',64),repeat('3',64),repeat('7',64),
      repeat('5',64),0,0,'local_transport_unavailable',
      3600,100,10,i,i,
      clock_timestamp()-interval '20 minutes'
        +make_interval(secs=>i::double precision/1000)
    );
  END LOOP;
END
$half_open$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','ca111111-1111-4111-8111-111111111111',true);
SELECT * FROM memory.claim_owner_v5_local_inference_job_v1(
  'ca920000-0000-4000-8000-000000000001',
  'ca920000-0000-4000-8000-000000000002',
  'ca400000-0000-4000-8000-000000000004',repeat('d',64),
  'relational_extraction','outcome-v2-test',900,2,
  'local_llama_cpp','outcome-v2',repeat('1',64),repeat('2',64),
  repeat('3',64),repeat('7',64),3600,100,10
) \gset half_one_
SELECT 1/((:'half_one_control_outcome'='reserved')::integer);

SELECT * FROM memory.claim_owner_v5_local_inference_job_v1(
  'ca920000-0000-4000-8000-000000000003',
  'ca920000-0000-4000-8000-000000000004',
  'ca400000-0000-4000-8000-000000000005',repeat('e',64),
  'relational_extraction','outcome-v2-test',900,2,
  'local_llama_cpp','outcome-v2',repeat('1',64),repeat('2',64),
  repeat('3',64),repeat('7',64),3600,100,10
) \gset half_block_
SELECT
  1/((:'half_block_control_outcome'='circuit_open')::integer);

SELECT * FROM memory.complete_owner_v5_local_inference_v1(
  'ca920000-0000-4000-8000-000000000005',
  :'half_one_reservation_event_id'::uuid,
  'ca920000-0000-4000-8000-000000000002',
  'ca400000-0000-4000-8000-000000000004',
  'rejected',1,'context_coreference_unresolved',NULL,NULL,NULL
) \gset half_complete_
SELECT * FROM memory.finalize_owner_v5_local_record_outcome_v2(
  'ca920000-0000-4000-8000-000000000006',
  'ca400000-0000-4000-8000-000000000004',
  :'half_one_lease_token'::uuid,'outcome-v2-test',repeat('d',64),
  :'half_complete_event_id'::uuid,'context_coreference_unresolved'
) \gset half_final_
SELECT
  1/((:'half_final_status'='skipped')::integer),
  1/((:'half_final_disposition'='deferred')::integer);

SELECT * FROM memory.claim_owner_v5_local_inference_job_v1(
  'ca920000-0000-4000-8000-000000000007',
  'ca920000-0000-4000-8000-000000000008',
  'ca400000-0000-4000-8000-000000000005',repeat('e',64),
  'relational_extraction','outcome-v2-test',900,2,
  'local_llama_cpp','outcome-v2',repeat('1',64),repeat('2',64),
  repeat('3',64),repeat('7',64),3600,100,10
) \gset half_recovered_
SELECT 1/((:'half_recovered_control_outcome'='reserved')::integer);

-- Sensitive/ambiguous material becomes review-required and replay is zero-write.
SELECT * FROM memory.claim_owner_v5_local_inference_job_v1(
  'ca930000-0000-4000-8000-000000000001',
  'ca930000-0000-4000-8000-000000000002',
  'ca400000-0000-4000-8000-000000000006',repeat('f',64),
  'relational_extraction','outcome-v2-test',900,2,
  'local_llama_cpp','outcome-v2',repeat('1',64),repeat('2',64),
  repeat('3',64),repeat('8',64),3600,100,10
) \gset review_claim_
SELECT * FROM memory.complete_owner_v5_local_inference_v1(
  'ca930000-0000-4000-8000-000000000003',
  :'review_claim_reservation_event_id'::uuid,
  'ca930000-0000-4000-8000-000000000002',
  'ca400000-0000-4000-8000-000000000006',
  'rejected',1,'sensitive_manual_review',NULL,NULL,NULL
) \gset review_complete_
SELECT * FROM memory.finalize_owner_v5_local_record_outcome_v2(
  'ca930000-0000-4000-8000-000000000004',
  'ca400000-0000-4000-8000-000000000006',
  :'review_claim_lease_token'::uuid,'outcome-v2-test',repeat('f',64),
  :'review_complete_event_id'::uuid,'sensitive_manual_review'
) \gset review_final_
SELECT
  1/((:'review_final_status'='review_required')::integer),
  1/((:'review_final_disposition'='review_required')::integer),
  1/((:'review_final_normalized_reason_code'=
    'sensitive_or_ambiguous_review')::integer);
SELECT * FROM memory.finalize_owner_v5_local_record_outcome_v2(
  'ca930000-0000-4000-8000-000000000004',
  'ca400000-0000-4000-8000-000000000006',
  :'review_claim_lease_token'::uuid,'outcome-v2-test',repeat('f',64),
  :'review_complete_event_id'::uuid,'sensitive_manual_review'
) \gset review_replay_
SELECT 1/((:'review_replay_apply_outcome'='replayed')::integer);

-- Cross-owner access cannot see or claim another owner's exact job.
SELECT set_config('app.user_id','cb111111-1111-4111-8111-111111111111',true);
DO $cross_owner$
BEGIN
  PERFORM * FROM memory.claim_owner_v5_local_inference_job_v1(
    'cb900000-0000-4000-8000-000000000001',
    'cb900000-0000-4000-8000-000000000002',
    'ca400000-0000-4000-8000-000000000007',repeat('0',64),
    'relational_extraction','outcome-v2-test',900,2,
    'local_llama_cpp','outcome-v2',repeat('1',64),repeat('2',64),
    repeat('3',64),repeat('8',64),3600,100,10
  );
  RAISE EXCEPTION 'cross-owner claim unexpectedly succeeded';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$cross_owner$;

SELECT set_config('app.user_id','ca111111-1111-4111-8111-111111111111',true);

-- Database status distinguishes active and circuit-blocked states.
SELECT 1/((
  memory.owner_v5_local_inference_status_v1(
    2,'local_llama_cpp','outcome-v2',repeat('1',64),repeat('2',64),
    repeat('3',64),repeat('6',64),3600,100,10,900
  )->>'database_state'='circuit_blocked'
)::integer);
SELECT 1/((
  memory.owner_v5_local_inference_status_v1(
    2,'local_llama_cpp','outcome-v2',repeat('1',64),repeat('2',64),
    repeat('3',64),repeat('8',64),3600,100,10,900
  )->>'database_state'='running'
)::integer);

RESET SESSION AUTHORIZATION;

-- No rejected/unreviewed outcome crossed into governed downstream stores.
SELECT
  1/((count(*)=:'before_claims')::integer)
FROM memory.claim;
SELECT
  1/((count(*)=:'before_observations')::integer)
FROM memory.observation;
SELECT
  1/((count(*)=:'before_entities')::integer)
FROM memory.entity;
SELECT
  1/((count(*)=:'before_projections')::integer)
FROM memory.projection_outbox;
SELECT
  1/((count(*)=:'before_bindings')::integer)
FROM memory.final_answer_memory_binding_v1;
SELECT
  1/((count(*)=:'before_packets')::integer)
FROM memory.evidence_extraction_packet_v5_local;

-- Existing and new provenance tables remain physically append-only.
DO $append_only$
BEGIN
  BEGIN
    UPDATE memory.v5_local_inference_event
    SET rejection_code='ordinary_semantic_rejection'
    WHERE owner_user_id='ca111111-1111-4111-8111-111111111111'
      AND action='completed';
    RAISE EXCEPTION 'local inference ledger update unexpectedly succeeded';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
  BEGIN
    UPDATE memory.v5_local_inference_outcome_event
    SET normalized_reason_code='ordinary_semantic_rejection'
    WHERE owner_user_id='ca111111-1111-4111-8111-111111111111';
    RAISE EXCEPTION 'outcome event update unexpectedly succeeded';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END
$append_only$;

ROLLBACK;

SELECT 'memory_v1_v5_2_circuit_outcome_v2: PASS' AS result;
