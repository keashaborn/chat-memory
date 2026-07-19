\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid regprocedure :=
    'memory.requeue_owner_local_compiler_failure_v2(uuid,uuid,text,uuid,uuid,integer,text,text,text)'::regprocedure;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc
    WHERE oid=function_oid AND prosecdef
      AND proowner='memory_v5_local_inference_maintainer'::regrole
      AND proconfig=ARRAY['search_path=pg_catalog']::text[]
  ) THEN
    RAISE EXCEPTION 'local compiler retry v2 function is unsafe';
  END IF;
  IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
     OR EXISTS (
       SELECT 1 FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid=function_oid AND acl.grantee=0
         AND acl.privilege_type='EXECUTE'
     ) THEN
    RAISE EXCEPTION 'local compiler retry v2 ACL is unsafe';
  END IF;
  IF has_table_privilege(
       'brains_app','memory.v5_local_inference_event','SELECT'
     ) OR has_table_privilege(
       'brains_app','memory.evidence_extraction_job','UPDATE'
     ) OR has_table_privilege(
       'brains_app','memory.evidence_extraction_event','INSERT'
     ) THEN
    RAISE EXCEPTION 'brains_app has unsafe direct table rights';
  END IF;
END
$security$;

BEGIN;

-- Two owners and two compiler-class failures exercise both allowed rejection
-- codes. All rows disappear with the transaction rollback.
SELECT set_config('app.user_id','ca111111-1111-4111-8111-111111111111',true);
INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,content,
  content_sha256,recorded_at,sensitivity,status,metadata
) VALUES (
  'ca200000-0000-4000-8000-000000000001',
  'ca111111-1111-4111-8111-111111111111','user_statement',
  'public.chat_log','compiler-retry-owner-a','Synthetic owner A.',
  repeat('a',64),'2026-07-19T06:00:00Z','medium','active','{}'
);
INSERT INTO memory.evidence_intake_terminal(
  terminal_id,owner_user_id,evidence_id,selector_version,outcome,reason_code,
  evidence_content_sha256,decision_fingerprint,actor_user_id,
  invoked_by_role,details
) VALUES (
  'ca300000-0000-4000-8000-000000000001',
  'ca111111-1111-4111-8111-111111111111',
  'ca200000-0000-4000-8000-000000000001','compiler_retry_v2_test',
  'dispatched','eligible_dispatched',repeat('a',64),repeat('1',64),
  'ca111111-1111-4111-8111-111111111111','sage',
  '{"route":"relational_extraction"}'
);
INSERT INTO memory.evidence_extraction_job(
  job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
  evidence_content_sha256,route,intake_reason_code,status,priority,attempts,
  last_error
) VALUES (
  'ca400000-0000-4000-8000-000000000001',
  'ca111111-1111-4111-8111-111111111111',
  'ca200000-0000-4000-8000-000000000001',
  'ca300000-0000-4000-8000-000000000001','compiler_retry_v2_test',
  repeat('a',64),'relational_extraction','eligible_unprocessed','skipped',10,1,
  'local_inference_rejected: invalid_structured_output'
);
INSERT INTO memory.evidence_extraction_event(
  event_id,owner_user_id,job_id,operation_id,event_type,from_status,to_status,
  actor_type,actor_ref,details
) VALUES (
  'ca700000-0000-4000-8000-000000000001',
  'ca111111-1111-4111-8111-111111111111',
  'ca400000-0000-4000-8000-000000000001',
  'ca800000-0000-4000-8000-000000000001','skipped','processing','skipped',
  'worker','compiler-retry-test',
  '{"attempt":1,"error_class":"local_inference_rejected"}'
);
INSERT INTO memory.v5_local_inference_event(
  event_id,owner_user_id,operation_id,run_id,job_id,target_job_sha256,
  target_content_sha256,reservation_event_id,action,outcome,provider_id,
  provider_version,provider_model_sha256,model_file_sha256,
  runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
  local_model_calls,external_model_calls,rejection_code,
  provider_output_sha256,validator_packet_sha256,packet_storage_sha256,
  rolling_window_seconds,max_reserved_jobs,failure_threshold,
  reserved_jobs_in_window,consecutive_rejections
) VALUES (
  'ca500000-0000-4000-8000-000000000001',
  'ca111111-1111-4111-8111-111111111111',
  'ca500000-0000-4000-8000-000000000011',
  'ca500000-0000-4000-8000-000000000012',
  'ca400000-0000-4000-8000-000000000001',repeat('2',64),repeat('a',64),
  NULL,'reserved','reserved','local_llama_cpp','v1',repeat('3',64),
  repeat('4',64),repeat('5',64),repeat('6',64),repeat('7',64),0,0,NULL,
  NULL,NULL,NULL,86400,12,3,1,0
),(
  'ca600000-0000-4000-8000-000000000001',
  'ca111111-1111-4111-8111-111111111111',
  'ca600000-0000-4000-8000-000000000011',
  'ca500000-0000-4000-8000-000000000012',
  'ca400000-0000-4000-8000-000000000001',repeat('2',64),repeat('a',64),
  'ca500000-0000-4000-8000-000000000001','completed','rejected',
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),repeat('7',64),1,0,'invalid_structured_output',
  NULL,NULL,NULL,86400,12,3,1,1
);

SELECT set_config('app.user_id','cb222222-2222-4222-8222-222222222222',true);
INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,content,
  content_sha256,recorded_at,sensitivity,status,metadata
) VALUES (
  'cb200000-0000-4000-8000-000000000001',
  'cb222222-2222-4222-8222-222222222222','user_statement',
  'public.chat_log','compiler-retry-owner-b','Synthetic owner B.',
  repeat('b',64),'2026-07-19T06:01:00Z','medium','active','{}'
);
INSERT INTO memory.evidence_intake_terminal(
  terminal_id,owner_user_id,evidence_id,selector_version,outcome,reason_code,
  evidence_content_sha256,decision_fingerprint,actor_user_id,
  invoked_by_role,details
) VALUES (
  'cb300000-0000-4000-8000-000000000001',
  'cb222222-2222-4222-8222-222222222222',
  'cb200000-0000-4000-8000-000000000001','compiler_retry_v2_test',
  'dispatched','eligible_dispatched',repeat('b',64),repeat('8',64),
  'cb222222-2222-4222-8222-222222222222','sage',
  '{"route":"relational_extraction"}'
);
INSERT INTO memory.evidence_extraction_job(
  job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
  evidence_content_sha256,route,intake_reason_code,status,priority,attempts,
  last_error
) VALUES (
  'cb400000-0000-4000-8000-000000000001',
  'cb222222-2222-4222-8222-222222222222',
  'cb200000-0000-4000-8000-000000000001',
  'cb300000-0000-4000-8000-000000000001','compiler_retry_v2_test',
  repeat('b',64),'relational_extraction','eligible_unprocessed','skipped',10,1,
  'local_inference_rejected: local_validation_rejected'
);
INSERT INTO memory.evidence_extraction_event(
  event_id,owner_user_id,job_id,operation_id,event_type,from_status,to_status,
  actor_type,actor_ref,details
) VALUES (
  'cb700000-0000-4000-8000-000000000001',
  'cb222222-2222-4222-8222-222222222222',
  'cb400000-0000-4000-8000-000000000001',
  'cb800000-0000-4000-8000-000000000001','skipped','processing','skipped',
  'worker','compiler-retry-test',
  '{"attempt":1,"error_class":"local_inference_rejected"}'
);
INSERT INTO memory.v5_local_inference_event(
  event_id,owner_user_id,operation_id,run_id,job_id,target_job_sha256,
  target_content_sha256,reservation_event_id,action,outcome,provider_id,
  provider_version,provider_model_sha256,model_file_sha256,
  runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
  local_model_calls,external_model_calls,rejection_code,
  provider_output_sha256,validator_packet_sha256,packet_storage_sha256,
  rolling_window_seconds,max_reserved_jobs,failure_threshold,
  reserved_jobs_in_window,consecutive_rejections
) VALUES (
  'cb500000-0000-4000-8000-000000000001',
  'cb222222-2222-4222-8222-222222222222',
  'cb500000-0000-4000-8000-000000000011',
  'cb500000-0000-4000-8000-000000000012',
  'cb400000-0000-4000-8000-000000000001',repeat('9',64),repeat('b',64),
  NULL,'reserved','reserved','local_llama_cpp','v1',repeat('3',64),
  repeat('4',64),repeat('5',64),repeat('6',64),repeat('7',64),0,0,NULL,
  NULL,NULL,NULL,86400,12,3,1,0
),(
  'cb600000-0000-4000-8000-000000000001',
  'cb222222-2222-4222-8222-222222222222',
  'cb600000-0000-4000-8000-000000000011',
  'cb500000-0000-4000-8000-000000000012',
  'cb400000-0000-4000-8000-000000000001',repeat('9',64),repeat('b',64),
  'cb500000-0000-4000-8000-000000000001','completed','rejected',
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),repeat('7',64),1,0,'local_validation_rejected',
  NULL,NULL,NULL,86400,12,3,1,1
);

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','ca111111-1111-4111-8111-111111111111',true);

DO $old_contract$
BEGIN
  PERFORM * FROM memory.requeue_owner_local_compiler_failure_v2(
    'ca900000-0000-4000-8000-000000000010',
    'ca400000-0000-4000-8000-000000000001',repeat('a',64),
    'ca800000-0000-4000-8000-000000000001',
    'ca600000-0000-4000-8000-000000000001',1,
    'invalid_structured_output',repeat('6',64),
    'memory_v1_local_policy_compiler_v3'
  );
  RAISE EXCEPTION 'obsolete compiler contract unexpectedly succeeded';
EXCEPTION WHEN invalid_parameter_value THEN NULL;
END
$old_contract$;

DO $cross_owner$
BEGIN
  PERFORM * FROM memory.requeue_owner_local_compiler_failure_v2(
    'ca900000-0000-4000-8000-000000000011',
    'cb400000-0000-4000-8000-000000000001',repeat('b',64),
    'cb800000-0000-4000-8000-000000000001',
    'cb600000-0000-4000-8000-000000000001',1,
    'local_validation_rejected',repeat('6',64),
    'memory_v1_local_policy_compiler_v4'
  );
  RAISE EXCEPTION 'cross-owner compiler retry unexpectedly succeeded';
EXCEPTION WHEN check_violation THEN NULL;
END
$cross_owner$;

SELECT * FROM memory.requeue_owner_local_compiler_failure_v2(
  'ca900000-0000-4000-8000-000000000001',
  'ca400000-0000-4000-8000-000000000001',repeat('a',64),
  'ca800000-0000-4000-8000-000000000001',
  'ca600000-0000-4000-8000-000000000001',1,
  'invalid_structured_output',repeat('6',64),
  'memory_v1_local_policy_compiler_v4'
) \gset retry_a_
SELECT
  1/((:'retry_a_status'='pending')::integer),
  1/((:'retry_a_attempts'='1')::integer),
  1/((:'retry_a_apply_outcome'='applied')::integer);
SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.requeue_owner_local_compiler_failure_v2(
  'ca900000-0000-4000-8000-000000000001',
  'ca400000-0000-4000-8000-000000000001',repeat('a',64),
  'ca800000-0000-4000-8000-000000000001',
  'ca600000-0000-4000-8000-000000000001',1,
  'invalid_structured_output',repeat('6',64),
  'memory_v1_local_policy_compiler_v4'
);

SELECT set_config('app.user_id','cb222222-2222-4222-8222-222222222222',true);
SELECT * FROM memory.requeue_owner_local_compiler_failure_v2(
  'cb900000-0000-4000-8000-000000000001',
  'cb400000-0000-4000-8000-000000000001',repeat('b',64),
  'cb800000-0000-4000-8000-000000000001',
  'cb600000-0000-4000-8000-000000000001',1,
  'local_validation_rejected',repeat('6',64),
  'memory_v1_local_policy_compiler_v4'
) \gset retry_b_
SELECT
  1/((:'retry_b_status'='pending')::integer),
  1/((:'retry_b_attempts'='1')::integer),
  1/((:'retry_b_apply_outcome'='applied')::integer);

RESET SESSION AUTHORIZATION;
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_event
WHERE owner_user_id='cb222222-2222-4222-8222-222222222222'::uuid
  AND operation_id='cb900000-0000-4000-8000-000000000001'::uuid
  AND actor_ref='local_compiler_retry_v2'
  AND details->>'retry_contract_version'=
      'memory_v1_local_policy_compiler_v4'
  AND details->>'prior_policy_compiler_sha256'=repeat('6',64);

ROLLBACK;

SELECT 'memory_v1_v5_local_compiler_retry_v2: PASS' AS result;
