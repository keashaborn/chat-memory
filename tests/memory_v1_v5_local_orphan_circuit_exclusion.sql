\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid constant regprocedure :=
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure;
  function_sha text;
BEGIN
  SELECT encode(public.digest(convert_to(pg_get_functiondef(function_oid),
    'UTF8'),'sha256'),'hex') INTO function_sha;
  IF function_sha<>'62175d9205544eaae20521dbb819d9c8c0838fbb94287c8abacdaed61ab93160' THEN
    RAISE EXCEPTION 'fingerprinted local circuit hash changed: %',function_sha;
  END IF;
  IF NOT EXISTS (
       SELECT 1 FROM pg_proc
       WHERE oid=function_oid AND prosecdef
         AND proowner='memory_v5_local_inference_maintainer'::regrole
         AND proconfig=ARRAY['search_path=pg_catalog']::text[]
     ) THEN
    RAISE EXCEPTION 'fingerprinted local circuit metadata is unsafe';
  END IF;
  IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
     OR EXISTS (
       SELECT 1 FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid=function_oid AND acl.grantee=0
         AND acl.privilege_type='EXECUTE'
     ) THEN
    RAISE EXCEPTION 'fingerprinted local circuit ACL is unsafe';
  END IF;
  IF has_table_privilege(
       'brains_app','memory.v5_local_inference_event','SELECT'
     ) OR has_table_privilege(
       'brains_app','memory.evidence_extraction_job','UPDATE'
     ) THEN
    RAISE EXCEPTION 'brains_app retained unsafe direct table rights';
  END IF;
END
$security$;

BEGIN;

SELECT set_config('app.user_id','ce111111-1111-4111-8111-111111111111',true);

INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,content,
  content_sha256,recorded_at,sensitivity,status,metadata
) VALUES
(
  'ce200000-0000-4000-8000-000000000001',
  'ce111111-1111-4111-8111-111111111111','user_statement',
  'public.chat_log','circuit-fingerprint-new','Synthetic new compiler job.',
  repeat('a',64),'2026-07-22T18:00:00Z','medium','active','{}'
),(
  'ce200000-0000-4000-8000-000000000002',
  'ce111111-1111-4111-8111-111111111111','user_statement',
  'public.chat_log','circuit-fingerprint-old','Synthetic old compiler job.',
  repeat('b',64),'2026-07-22T18:01:00Z','medium','active','{}'
);

INSERT INTO memory.evidence_intake_terminal(
  terminal_id,owner_user_id,evidence_id,selector_version,outcome,reason_code,
  evidence_content_sha256,decision_fingerprint,actor_user_id,
  invoked_by_role,details
) VALUES
(
  'ce300000-0000-4000-8000-000000000001',
  'ce111111-1111-4111-8111-111111111111',
  'ce200000-0000-4000-8000-000000000001','circuit_fingerprint_test',
  'dispatched','eligible_dispatched',repeat('a',64),repeat('1',64),
  'ce111111-1111-4111-8111-111111111111','sage',
  '{"route":"relational_extraction"}'
),(
  'ce300000-0000-4000-8000-000000000002',
  'ce111111-1111-4111-8111-111111111111',
  'ce200000-0000-4000-8000-000000000002','circuit_fingerprint_test',
  'dispatched','eligible_dispatched',repeat('b',64),repeat('2',64),
  'ce111111-1111-4111-8111-111111111111','sage',
  '{"route":"relational_extraction"}'
);

INSERT INTO memory.evidence_extraction_job(
  job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
  evidence_content_sha256,route,intake_reason_code,status,priority
) VALUES
(
  'ce400000-0000-4000-8000-000000000001',
  'ce111111-1111-4111-8111-111111111111',
  'ce200000-0000-4000-8000-000000000001',
  'ce300000-0000-4000-8000-000000000001','circuit_fingerprint_test',
  repeat('a',64),'relational_extraction','eligible_unprocessed','pending',10
),(
  'ce400000-0000-4000-8000-000000000002',
  'ce111111-1111-4111-8111-111111111111',
  'ce200000-0000-4000-8000-000000000002',
  'ce300000-0000-4000-8000-000000000002','circuit_fingerprint_test',
  repeat('b',64),'relational_extraction','eligible_unprocessed','pending',10
);

INSERT INTO memory.evidence_extraction_event(
  owner_user_id,job_id,event_type,from_status,to_status,actor_type,
  actor_ref,details
) VALUES
(
  'ce111111-1111-4111-8111-111111111111',
  'ce400000-0000-4000-8000-000000000001','queued',NULL,'pending',
  'system','circuit-fingerprint-test','{}'
),(
  'ce111111-1111-4111-8111-111111111111',
  'ce400000-0000-4000-8000-000000000002','queued',NULL,'pending',
  'system','circuit-fingerprint-test','{}'
);

INSERT INTO memory.v5_local_inference_event(
  event_id,owner_user_id,operation_id,run_id,job_id,target_job_sha256,
  target_content_sha256,reservation_event_id,action,outcome,provider_id,
  provider_version,provider_model_sha256,model_file_sha256,
  runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
  local_model_calls,external_model_calls,rejection_code,
  rolling_window_seconds,max_reserved_jobs,failure_threshold,
  reserved_jobs_in_window,consecutive_rejections
) VALUES
(
  'ce500000-0000-4000-8000-000000000001',
  'ce111111-1111-4111-8111-111111111111',
  'ce510000-0000-4000-8000-000000000001',
  'ce520000-0000-4000-8000-000000000001',
  'ce400000-0000-4000-8000-000000000002',repeat('2',64),repeat('c',64),
  NULL,'reserved','reserved','local_llama_cpp','v1',repeat('3',64),
  repeat('4',64),repeat('5',64),repeat('6',64),repeat('7',64),0,0,NULL,
  86400,12,3,1,0
),(
  'ce600000-0000-4000-8000-000000000001',
  'ce111111-1111-4111-8111-111111111111',
  'ce610000-0000-4000-8000-000000000001',
  'ce520000-0000-4000-8000-000000000001',
  'ce400000-0000-4000-8000-000000000002',repeat('2',64),repeat('c',64),
  'ce500000-0000-4000-8000-000000000001','completed','rejected',
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),repeat('7',64),1,0,'local_incomplete_response',
  86400,12,3,1,1
),(
  'ce500000-0000-4000-8000-000000000002',
  'ce111111-1111-4111-8111-111111111111',
  'ce510000-0000-4000-8000-000000000002',
  'ce520000-0000-4000-8000-000000000002',
  'ce400000-0000-4000-8000-000000000002',repeat('2',64),repeat('c',64),
  NULL,'reserved','reserved','local_llama_cpp','v1',repeat('3',64),
  repeat('4',64),repeat('5',64),repeat('6',64),repeat('7',64),0,0,NULL,
  86400,12,3,2,1
),(
  'ce600000-0000-4000-8000-000000000002',
  'ce111111-1111-4111-8111-111111111111',
  'ce610000-0000-4000-8000-000000000002',
  'ce520000-0000-4000-8000-000000000002',
  'ce400000-0000-4000-8000-000000000002',repeat('2',64),repeat('c',64),
  'ce500000-0000-4000-8000-000000000002','completed','rejected',
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),repeat('7',64),1,0,'local_incomplete_response',
  86400,12,3,2,2
),(
  'ce500000-0000-4000-8000-000000000003',
  'ce111111-1111-4111-8111-111111111111',
  'ce510000-0000-4000-8000-000000000003',
  'ce520000-0000-4000-8000-000000000003',
  'ce400000-0000-4000-8000-000000000002',repeat('2',64),repeat('c',64),
  NULL,'reserved','reserved','local_llama_cpp','v1',repeat('3',64),
  repeat('4',64),repeat('5',64),repeat('6',64),repeat('7',64),0,0,NULL,
  86400,12,3,3,2
),(
  'ce600000-0000-4000-8000-000000000003',
  'ce111111-1111-4111-8111-111111111111',
  'ce610000-0000-4000-8000-000000000003',
  'ce520000-0000-4000-8000-000000000003',
  'ce400000-0000-4000-8000-000000000002',repeat('2',64),repeat('c',64),
  'ce500000-0000-4000-8000-000000000003','completed','rejected',
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),repeat('7',64),1,0,'local_incomplete_response',
  86400,12,3,3,3
);

-- Historical audit reconstruction is not a runtime/model rejection. These
-- three rows share the new fingerprint and must not open its circuit.
INSERT INTO memory.v5_local_inference_event(
  event_id,owner_user_id,operation_id,run_id,job_id,target_job_sha256,
  target_content_sha256,reservation_event_id,action,outcome,provider_id,
  provider_version,provider_model_sha256,model_file_sha256,
  runtime_revision_sha256,policy_compiler_sha256,worker_id_sha256,
  local_model_calls,external_model_calls,rejection_code,
  rolling_window_seconds,max_reserved_jobs,failure_threshold,
  reserved_jobs_in_window,consecutive_rejections
) VALUES
(
  'cf500000-0000-4000-8000-000000000001',
  'ce111111-1111-4111-8111-111111111111',
  'cf510000-0000-4000-8000-000000000001',
  'cf520000-0000-4000-8000-000000000001',
  'ce400000-0000-4000-8000-000000000001',repeat('1',64),repeat('a',64),
  NULL,'reserved','reserved','local_llama_cpp','v1',repeat('3',64),
  repeat('4',64),repeat('5',64),repeat('d',64),repeat('7',64),0,0,NULL,
  86400,12,3,4,0
),(
  'cf600000-0000-4000-8000-000000000001',
  'ce111111-1111-4111-8111-111111111111',
  'cf610000-0000-4000-8000-000000000001',
  'cf520000-0000-4000-8000-000000000001',
  'ce400000-0000-4000-8000-000000000001',repeat('1',64),repeat('a',64),
  'cf500000-0000-4000-8000-000000000001','completed','rejected',
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('d',64),repeat('7',64),1,0,'local_worker_abandoned',
  86400,12,3,4,1
),(
  'cf500000-0000-4000-8000-000000000002',
  'ce111111-1111-4111-8111-111111111111',
  'cf510000-0000-4000-8000-000000000002',
  'cf520000-0000-4000-8000-000000000002',
  'ce400000-0000-4000-8000-000000000001',repeat('1',64),repeat('a',64),
  NULL,'reserved','reserved','local_llama_cpp','v1',repeat('3',64),
  repeat('4',64),repeat('5',64),repeat('d',64),repeat('7',64),0,0,NULL,
  86400,12,3,5,1
),(
  'cf600000-0000-4000-8000-000000000002',
  'ce111111-1111-4111-8111-111111111111',
  'cf610000-0000-4000-8000-000000000002',
  'cf520000-0000-4000-8000-000000000002',
  'ce400000-0000-4000-8000-000000000001',repeat('1',64),repeat('a',64),
  'cf500000-0000-4000-8000-000000000002','completed','rejected',
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('d',64),repeat('7',64),1,0,'local_worker_abandoned',
  86400,12,3,5,2
),(
  'cf500000-0000-4000-8000-000000000003',
  'ce111111-1111-4111-8111-111111111111',
  'cf510000-0000-4000-8000-000000000003',
  'cf520000-0000-4000-8000-000000000003',
  'ce400000-0000-4000-8000-000000000001',repeat('1',64),repeat('a',64),
  NULL,'reserved','reserved','local_llama_cpp','v1',repeat('3',64),
  repeat('4',64),repeat('5',64),repeat('d',64),repeat('7',64),0,0,NULL,
  86400,12,3,6,2
),(
  'cf600000-0000-4000-8000-000000000003',
  'ce111111-1111-4111-8111-111111111111',
  'cf610000-0000-4000-8000-000000000003',
  'cf520000-0000-4000-8000-000000000003',
  'ce400000-0000-4000-8000-000000000001',repeat('1',64),repeat('a',64),
  'cf500000-0000-4000-8000-000000000003','completed','rejected',
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('d',64),repeat('7',64),1,0,'local_worker_abandoned',
  86400,12,3,6,3
);

SET SESSION AUTHORIZATION brains_app;

-- A new fingerprint is not blocked by older failures or orphan audit rows.
SELECT * FROM memory.claim_owner_v5_local_inference_job_v1(
  'ce900000-0000-4000-8000-000000000001',
  'ce900000-0000-4000-8000-000000000002',
  'ce400000-0000-4000-8000-000000000001',repeat('a',64),
  'relational_extraction','circuit-fingerprint-test',300,1,
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('d',64),86400,12,3
) \gset new_
SELECT
  1/((:'new_control_outcome'='reserved')::integer),
  1/((:'new_consecutive_rejections'='0')::integer),
  1/((:'new_job_id'='ce400000-0000-4000-8000-000000000001')::integer);

-- The same three failures still open the circuit for their own fingerprint.
SELECT
  1/((job_id IS NULL)::integer),
  1/((control_outcome='circuit_open')::integer),
  1/((consecutive_rejections=3)::integer)
FROM memory.claim_owner_v5_local_inference_job_v1(
  'ce910000-0000-4000-8000-000000000001',
  'ce910000-0000-4000-8000-000000000002',
  'ce400000-0000-4000-8000-000000000002',repeat('b',64),
  'relational_extraction','circuit-fingerprint-test',300,1,
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),86400,12,3
);

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_circuit_fingerprint: PASS' AS result;
