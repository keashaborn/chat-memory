\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid regprocedure :=
    'memory.requeue_owner_local_zero_call_validation_failure_v1(uuid,uuid,text,uuid,uuid,integer,text)'::regprocedure;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc
    WHERE oid=function_oid AND prosecdef
      AND proowner='memory_v5_local_inference_maintainer'::regrole
      AND proconfig=ARRAY['search_path=pg_catalog']::text[]
  ) THEN
    RAISE EXCEPTION 'zero-call validation retry function is unsafe';
  END IF;
  IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
     OR EXISTS (
       SELECT 1 FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid=function_oid AND acl.grantee=0
         AND acl.privilege_type='EXECUTE'
     ) THEN
    RAISE EXCEPTION 'zero-call validation retry ACL is unsafe';
  END IF;
  IF has_table_privilege(
       'brains_app','memory.evidence_extraction_job','UPDATE'
     ) OR has_table_privilege(
       'brains_app','memory.evidence_extraction_event','INSERT'
     ) OR has_table_privilege(
       'brains_app','memory.v5_local_inference_event','SELECT'
     ) THEN
    RAISE EXCEPTION 'brains_app has unsafe direct table rights';
  END IF;
END
$security$;

BEGIN;

SELECT set_config(
  'app.user_id','fc111111-1111-4111-8111-111111111111',true
);
INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,content,
  content_sha256,recorded_at,sensitivity,status,metadata
) VALUES (
  'fc200000-0000-4000-8000-000000000001',
  'fc111111-1111-4111-8111-111111111111','user_statement',
  'public.chat_log','zero-call-validation-owner-a',
  'Synthetic zero-call validation fixture.',repeat('a',64),
  '2026-07-26T00:00:00Z','medium','active','{}'
);
INSERT INTO memory.evidence_intake_terminal(
  terminal_id,owner_user_id,evidence_id,selector_version,outcome,reason_code,
  evidence_content_sha256,decision_fingerprint,actor_user_id,
  invoked_by_role,details
) VALUES (
  'fc300000-0000-4000-8000-000000000001',
  'fc111111-1111-4111-8111-111111111111',
  'fc200000-0000-4000-8000-000000000001',
  'zero_call_validation_test_v1','dispatched','eligible_dispatched',
  repeat('a',64),repeat('1',64),
  'fc111111-1111-4111-8111-111111111111','sage',
  '{"route":"relational_extraction"}'
);
INSERT INTO memory.evidence_extraction_job(
  job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
  evidence_content_sha256,route,intake_reason_code,status,priority,attempts,
  last_error
) VALUES (
  'fc400000-0000-4000-8000-000000000001',
  'fc111111-1111-4111-8111-111111111111',
  'fc200000-0000-4000-8000-000000000001',
  'fc300000-0000-4000-8000-000000000001',
  'zero_call_validation_test_v1',repeat('a',64),
  'relational_extraction','eligible_unprocessed','skipped',10,1,
  'local_inference_rejected: local_validation_rejected'
);
INSERT INTO memory.evidence_extraction_event(
  event_id,owner_user_id,job_id,operation_id,event_type,from_status,to_status,
  actor_type,actor_ref,details
) VALUES (
  'fc800000-0000-4000-8000-000000000001',
  'fc111111-1111-4111-8111-111111111111',
  'fc400000-0000-4000-8000-000000000001',
  'fc800000-0000-4000-8000-000000000002',
  'skipped','processing','skipped','worker','zero-call-validation-test',
  '{"error_class":"local_inference_rejected","attempt":1}'
);
INSERT INTO memory.v5_local_inference_event(
  event_id,owner_user_id,operation_id,run_id,job_id,target_job_sha256,
  target_content_sha256,reservation_event_id,action,outcome,provider_id,
  provider_version,provider_model_sha256,model_file_sha256,
  runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
  local_model_calls,external_model_calls,rejection_code,
  rolling_window_seconds,max_reserved_jobs,failure_threshold,
  reserved_jobs_in_window,consecutive_rejections
) VALUES (
  'fc500000-0000-4000-8000-000000000001',
  'fc111111-1111-4111-8111-111111111111',
  'fc500000-0000-4000-8000-000000000002',
  'fc500000-0000-4000-8000-000000000003',
  'fc400000-0000-4000-8000-000000000001',
  repeat('2',64),repeat('a',64),NULL,'reserved','reserved',
  'local_llama_cpp','zero-call-validation-test',repeat('3',64),
  repeat('4',64),repeat('5',64),repeat('6',64),repeat('7',64),
  0,0,NULL,86400,12,3,1,0
),(
  'fc600000-0000-4000-8000-000000000001',
  'fc111111-1111-4111-8111-111111111111',
  'fc600000-0000-4000-8000-000000000002',
  'fc500000-0000-4000-8000-000000000003',
  'fc400000-0000-4000-8000-000000000001',
  repeat('2',64),repeat('a',64),
  'fc500000-0000-4000-8000-000000000001',
  'completed','rejected','local_llama_cpp','zero-call-validation-test',
  repeat('3',64),repeat('4',64),repeat('5',64),repeat('6',64),
  repeat('7',64),0,0,'local_validation_rejected',86400,12,3,1,1
);

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','fb222222-2222-4222-8222-222222222222',true
);
DO $cross_owner$
BEGIN
  PERFORM * FROM
    memory.requeue_owner_local_zero_call_validation_failure_v1(
      'fc900000-0000-4000-8000-000000000001',
      'fc400000-0000-4000-8000-000000000001',repeat('a',64),
      'fc800000-0000-4000-8000-000000000002',
      'fc600000-0000-4000-8000-000000000001',1,
      'evidence_context_window_compiler_repair'
    );
  RAISE EXCEPTION 'cross-owner zero-call retry unexpectedly succeeded';
EXCEPTION WHEN check_violation THEN NULL;
END
$cross_owner$;

SELECT set_config(
  'app.user_id','fc111111-1111-4111-8111-111111111111',true
);
SELECT * FROM
  memory.requeue_owner_local_zero_call_validation_failure_v1(
    'fc900000-0000-4000-8000-000000000001',
    'fc400000-0000-4000-8000-000000000001',repeat('a',64),
    'fc800000-0000-4000-8000-000000000002',
    'fc600000-0000-4000-8000-000000000001',1,
    'evidence_context_window_compiler_repair'
  ) \gset applied_
SELECT
  1/((:'applied_status'='pending')::integer),
  1/((:'applied_attempts'='1')::integer),
  1/((:'applied_apply_outcome'='applied')::integer);
SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.requeue_owner_local_zero_call_validation_failure_v1(
  'fc900000-0000-4000-8000-000000000001',
  'fc400000-0000-4000-8000-000000000001',repeat('a',64),
  'fc800000-0000-4000-8000-000000000002',
  'fc600000-0000-4000-8000-000000000001',1,
  'evidence_context_window_compiler_repair'
);

RESET SESSION AUTHORIZATION;
SELECT
  1/((status='pending')::integer),
  1/((attempts=1)::integer),
  1/((last_error IS NULL)::integer),
  1/((lease_token IS NULL AND lease_expires_at IS NULL)::integer)
FROM memory.evidence_extraction_job
WHERE owner_user_id='fc111111-1111-4111-8111-111111111111'::uuid
  AND job_id='fc400000-0000-4000-8000-000000000001'::uuid;
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_event
WHERE owner_user_id='fc111111-1111-4111-8111-111111111111'::uuid
  AND operation_id='fc900000-0000-4000-8000-000000000001'::uuid
  AND actor_ref='local_zero_call_validation_retry'
  AND from_status='skipped' AND to_status='pending';

ROLLBACK;
