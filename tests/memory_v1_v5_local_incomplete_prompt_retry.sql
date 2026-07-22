\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid regprocedure :=
    'memory.requeue_owner_local_incomplete_failure_v1(uuid,uuid,text,uuid,uuid,integer,text)'::regprocedure;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc
    WHERE oid=function_oid AND prosecdef
      AND proowner='memory_v5_local_inference_maintainer'::regrole
      AND proconfig=ARRAY['search_path=pg_catalog']::text[]
  ) THEN
    RAISE EXCEPTION 'local incomplete retry function is unsafe';
  END IF;
  IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
     OR EXISTS (
       SELECT 1 FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid=function_oid AND acl.grantee=0
         AND acl.privilege_type='EXECUTE'
     ) THEN
    RAISE EXCEPTION 'local incomplete retry ACL is unsafe';
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

SELECT set_config('app.user_id','da111111-1111-4111-8111-111111111111',true);
INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,content,
  content_sha256,recorded_at,sensitivity,status,metadata
) VALUES (
  'da200000-0000-4000-8000-000000000001',
  'da111111-1111-4111-8111-111111111111','user_statement',
  'public.chat_log','incomplete-retry-owner-a','Synthetic nuanced belief.',
  repeat('a',64),'2026-07-22T17:00:00Z','medium','active','{}'
);
INSERT INTO memory.evidence_intake_terminal(
  terminal_id,owner_user_id,evidence_id,selector_version,outcome,reason_code,
  evidence_content_sha256,decision_fingerprint,actor_user_id,
  invoked_by_role,details
) VALUES (
  'da300000-0000-4000-8000-000000000001',
  'da111111-1111-4111-8111-111111111111',
  'da200000-0000-4000-8000-000000000001','incomplete_retry_test_v1',
  'dispatched','eligible_dispatched',repeat('a',64),repeat('1',64),
  'da111111-1111-4111-8111-111111111111','sage',
  '{"route":"relational_extraction"}'
);
INSERT INTO memory.evidence_extraction_job(
  job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
  evidence_content_sha256,route,intake_reason_code,status,priority,attempts,
  last_error
) VALUES (
  'da400000-0000-4000-8000-000000000001',
  'da111111-1111-4111-8111-111111111111',
  'da200000-0000-4000-8000-000000000001',
  'da300000-0000-4000-8000-000000000001','incomplete_retry_test_v1',
  repeat('a',64),'relational_extraction','eligible_unprocessed','skipped',10,1,
  'local_inference_rejected: local_incomplete_response'
);
INSERT INTO memory.evidence_extraction_event(
  event_id,owner_user_id,job_id,operation_id,event_type,from_status,to_status,
  actor_type,actor_ref,details
) VALUES (
  'da700000-0000-4000-8000-000000000001',
  'da111111-1111-4111-8111-111111111111',
  'da400000-0000-4000-8000-000000000001',
  'da800000-0000-4000-8000-000000000001','skipped','processing','skipped',
  'worker','incomplete-retry-test',
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
  'da500000-0000-4000-8000-000000000001',
  'da111111-1111-4111-8111-111111111111',
  'da500000-0000-4000-8000-000000000011',
  'da500000-0000-4000-8000-000000000012',
  'da400000-0000-4000-8000-000000000001',repeat('2',64),repeat('a',64),
  NULL,'reserved','reserved','local_llama_cpp','v1',repeat('3',64),
  repeat('4',64),repeat('5',64),repeat('6',64),repeat('7',64),0,0,NULL,
  NULL,NULL,NULL,86400,12,3,1,0
),(
  'da600000-0000-4000-8000-000000000001',
  'da111111-1111-4111-8111-111111111111',
  'da600000-0000-4000-8000-000000000011',
  'da500000-0000-4000-8000-000000000012',
  'da400000-0000-4000-8000-000000000001',repeat('2',64),repeat('a',64),
  'da500000-0000-4000-8000-000000000001','completed','rejected',
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),repeat('7',64),1,0,'local_incomplete_response',
  NULL,NULL,NULL,86400,12,3,1,1
);

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','da111111-1111-4111-8111-111111111111',true);

DO $wrong_reason$
BEGIN
  PERFORM * FROM memory.requeue_owner_local_incomplete_failure_v1(
    'da900000-0000-4000-8000-000000000010',
    'da400000-0000-4000-8000-000000000001',repeat('a',64),
    'da800000-0000-4000-8000-000000000001',
    'da600000-0000-4000-8000-000000000001',1,
    'private_gpu_route_recovery'
  );
  RAISE EXCEPTION 'wrong incomplete retry reason unexpectedly succeeded';
EXCEPTION WHEN invalid_parameter_value THEN NULL;
END
$wrong_reason$;

SELECT set_config('app.user_id','db222222-2222-4222-8222-222222222222',true);
DO $cross_owner$
BEGIN
  PERFORM * FROM memory.requeue_owner_local_incomplete_failure_v1(
    'db900000-0000-4000-8000-000000000001',
    'da400000-0000-4000-8000-000000000001',repeat('a',64),
    'da800000-0000-4000-8000-000000000001',
    'da600000-0000-4000-8000-000000000001',1,
    'bounded_output_ceiling_increase'
  );
  RAISE EXCEPTION 'cross-owner incomplete retry unexpectedly succeeded';
EXCEPTION WHEN check_violation THEN NULL;
END
$cross_owner$;

SELECT set_config('app.user_id','da111111-1111-4111-8111-111111111111',true);

DO $legacy_reason_compatibility$
BEGIN
  PERFORM * FROM memory.requeue_owner_local_incomplete_failure_v1(
    'da900000-0000-4000-8000-000000000011',
    'da400000-0000-4000-8000-000000000001',repeat('a',64),
    'da800000-0000-4000-8000-000000000001',
    'da600000-0000-4000-8000-000000000001',1,
    'bounded_output_ceiling_increase'
  );
  RAISE EXCEPTION 'rollback successful compatibility probe';
EXCEPTION WHEN raise_exception THEN NULL;
END
$legacy_reason_compatibility$;

SELECT * FROM memory.requeue_owner_local_incomplete_failure_v1(
  'da900000-0000-4000-8000-000000000001',
  'da400000-0000-4000-8000-000000000001',repeat('a',64),
  'da800000-0000-4000-8000-000000000001',
  'da600000-0000-4000-8000-000000000001',1,
  'bounded_prompt_compiler_upgrade'
) \gset retry_
SELECT
  1/((:'retry_status'='pending')::integer),
  1/((:'retry_attempts'='1')::integer),
  1/((:'retry_apply_outcome'='applied')::integer);
SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.requeue_owner_local_incomplete_failure_v1(
  'da900000-0000-4000-8000-000000000001',
  'da400000-0000-4000-8000-000000000001',repeat('a',64),
  'da800000-0000-4000-8000-000000000001',
  'da600000-0000-4000-8000-000000000001',1,
  'bounded_prompt_compiler_upgrade'
);

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_incomplete_prompt_retry: PASS' AS result;
