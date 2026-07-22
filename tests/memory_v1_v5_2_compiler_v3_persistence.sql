\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid regprocedure;
BEGIN
  FOREACH function_oid IN ARRAY ARRAY[
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure,
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure,
    'memory.requeue_owner_v5_2_persistence_mismatch_v1(uuid,uuid,text,uuid,uuid,integer,text)'::regprocedure
  ] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_proc WHERE oid=function_oid AND prosecdef
        AND proowner='memory_v5_local_inference_maintainer'::regrole
    ) OR NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
       OR EXISTS (
         SELECT 1 FROM pg_proc AS procedure
         CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
         WHERE procedure.oid=function_oid AND acl.grantee=0
           AND acl.privilege_type='EXECUTE'
       ) THEN
      RAISE EXCEPTION 'V5.2 compiler-v3 function % is unsafe',function_oid;
    END IF;
  END LOOP;
  IF has_table_privilege(
       'brains_app','memory.v5_local_inference_event','SELECT'
     ) OR has_table_privilege(
       'brains_app','memory.evidence_extraction_job','UPDATE'
     ) THEN
    RAISE EXCEPTION 'brains_app has unsafe direct V5.2 table rights';
  END IF;
END
$security$;

BEGIN;
SELECT set_config('app.user_id','cf111111-1111-4111-8111-111111111111',true);
INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,content,
  content_sha256,recorded_at,sensitivity,status,metadata
) VALUES (
  'cf200000-0000-4000-8000-000000000001',
  'cf111111-1111-4111-8111-111111111111','user_statement',
  'public.chat_log','compiler-v3-test','Synthetic compiler v3 test.',
  repeat('a',64),'2026-07-22T19:00:00Z','medium','active','{}'
);
INSERT INTO memory.evidence_intake_terminal(
  terminal_id,owner_user_id,evidence_id,selector_version,outcome,reason_code,
  evidence_content_sha256,decision_fingerprint,actor_user_id,
  invoked_by_role,details
) VALUES (
  'cf300000-0000-4000-8000-000000000001',
  'cf111111-1111-4111-8111-111111111111',
  'cf200000-0000-4000-8000-000000000001','compiler_v3_test',
  'dispatched','eligible_dispatched',repeat('a',64),repeat('1',64),
  'cf111111-1111-4111-8111-111111111111','sage',
  '{"route":"relational_extraction"}'
);
INSERT INTO memory.evidence_extraction_job(
  job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
  evidence_content_sha256,route,intake_reason_code,status,priority,attempts,
  last_error
) VALUES (
  'cf400000-0000-4000-8000-000000000001',
  'cf111111-1111-4111-8111-111111111111',
  'cf200000-0000-4000-8000-000000000001',
  'cf300000-0000-4000-8000-000000000001','compiler_v3_test',repeat('a',64),
  'relational_extraction','eligible_unprocessed','skipped',10,3,
  'local_inference_rejected: local_persistence_contract_mismatch'
);
INSERT INTO memory.evidence_extraction_event(
  event_id,owner_user_id,job_id,operation_id,event_type,from_status,to_status,
  actor_type,actor_ref,details
) VALUES (
  'cf700000-0000-4000-8000-000000000001',
  'cf111111-1111-4111-8111-111111111111',
  'cf400000-0000-4000-8000-000000000001',
  'cf710000-0000-4000-8000-000000000001','skipped','processing','skipped',
  'worker','compiler-v3-test',
  '{"attempt":3,"error_class":"local_inference_rejected"}'
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
  'cf500000-0000-4000-8000-000000000001',
  'cf111111-1111-4111-8111-111111111111',
  'cf510000-0000-4000-8000-000000000001',
  'cf520000-0000-4000-8000-000000000001',
  'cf400000-0000-4000-8000-000000000001',repeat('2',64),repeat('a',64),
  NULL,'reserved','reserved','local_llama_cpp','v1',repeat('3',64),
  repeat('4',64),repeat('5',64),repeat('6',64),repeat('7',64),0,0,NULL,
  86400,12,3,1,0
),(
  'cf600000-0000-4000-8000-000000000001',
  'cf111111-1111-4111-8111-111111111111',
  'cf610000-0000-4000-8000-000000000001',
  'cf520000-0000-4000-8000-000000000001',
  'cf400000-0000-4000-8000-000000000001',repeat('2',64),repeat('a',64),
  'cf500000-0000-4000-8000-000000000001','completed','rejected',
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),repeat('7',64),1,0,
  'local_persistence_contract_mismatch',86400,12,3,1,1
);

SET SESSION AUTHORIZATION brains_app;

-- Compiler v3 passes input validation; the synthetic lease must fail later.
DO $persistence_v3$
BEGIN
  PERFORM * FROM memory.persist_owner_v5_2_local_packet_v1(
    'cf800000-0000-4000-8000-000000000001',
    'cf800000-0000-4000-8000-000000000002',
    'cf400000-0000-4000-8000-000000000001',
    'cf800000-0000-4000-8000-000000000003','compiler-v3-test',repeat('a',64),
    'v1',repeat('3',64),repeat('4',64),repeat('5',64),
    'fb7d46cbc52f16c0cb38c74cda2fc46c7f3343fda68dde1aed88ace4ab4b378e',
    repeat('8',64),repeat('9',64),
    '{"contract_version":"memory_v1_relational_extraction_v5_2","predicate_registry_version":"memory_predicate_registry_v5_2","entity_mentions":[],"observations":[],"comparison_hints":[],"deferrals":[],"packet_findings":[]}'::jsonb,
    false,1
  );
  RAISE EXCEPTION 'synthetic V5.2 persistence unexpectedly succeeded';
EXCEPTION
  WHEN check_violation THEN NULL;
  WHEN invalid_parameter_value THEN
    RAISE EXCEPTION 'compiler v3 was rejected by persistence input validation';
END
$persistence_v3$;

-- The bounded ceiling accepts attempt 4, then this subtransaction rolls back.
DO $claim_attempt_four$
BEGIN
  PERFORM * FROM memory.claim_owner_v5_local_inference_job_v1(
    'cf900000-0000-4000-8000-000000000001',
    'cf900000-0000-4000-8000-000000000002',
    'cf400000-0000-4000-8000-000000000001',repeat('a',64),
    'relational_extraction','compiler-v3-test',300,4,
    'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
    repeat('d',64),86400,12,3
  );
  RAISE EXCEPTION 'rollback successful attempt-four probe';
EXCEPTION
  WHEN raise_exception THEN NULL;
  WHEN check_violation THEN NULL;
  WHEN invalid_parameter_value THEN
    RAISE EXCEPTION 'attempt four was rejected by claim input validation';
END
$claim_attempt_four$;

SELECT set_config('app.user_id','cf222222-2222-4222-8222-222222222222',true);
DO $cross_owner$
BEGIN
  PERFORM * FROM memory.requeue_owner_v5_2_persistence_mismatch_v1(
    'cf900000-0000-4000-8000-000000000011',
    'cf400000-0000-4000-8000-000000000001',repeat('a',64),
    'cf710000-0000-4000-8000-000000000001',
    'cf600000-0000-4000-8000-000000000001',3,
    'semantic_compiler_v3_persistence_compatibility'
  );
  RAISE EXCEPTION 'cross-owner V5.2 retry unexpectedly succeeded';
EXCEPTION WHEN check_violation THEN NULL;
END
$cross_owner$;

SELECT set_config('app.user_id','cf111111-1111-4111-8111-111111111111',true);
SELECT * FROM memory.requeue_owner_v5_2_persistence_mismatch_v1(
  'cf900000-0000-4000-8000-000000000021',
  'cf400000-0000-4000-8000-000000000001',repeat('a',64),
  'cf710000-0000-4000-8000-000000000001',
  'cf600000-0000-4000-8000-000000000001',3,
  'semantic_compiler_v3_persistence_compatibility'
) \gset retry_
SELECT
  1/((:'retry_status'='pending')::integer),
  1/((:'retry_attempts'='3')::integer),
  1/((:'retry_apply_outcome'='applied')::integer);
SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.requeue_owner_v5_2_persistence_mismatch_v1(
  'cf900000-0000-4000-8000-000000000021',
  'cf400000-0000-4000-8000-000000000001',repeat('a',64),
  'cf710000-0000-4000-8000-000000000001',
  'cf600000-0000-4000-8000-000000000001',3,
  'semantic_compiler_v3_persistence_compatibility'
);

RESET SESSION AUTHORIZATION;
ROLLBACK;
SELECT 'memory_v1_v5_2_compiler_v3_persistence: PASS' AS result;
