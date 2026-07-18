\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid regprocedure;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_local_inference_maintainer'
      AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolinherit AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'memory_v5_local_inference_maintainer role is unsafe';
  END IF;

  FOREACH function_oid IN ARRAY ARRAY[
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure,
    'memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure,
    'memory.complete_owner_v5_local_inference_v1(uuid,uuid,uuid,uuid,text,integer,text,text,text,text)'::regprocedure
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_proc
      WHERE oid=function_oid AND prosecdef
        AND proowner='memory_v5_local_inference_maintainer'::regrole
        AND proconfig=ARRAY['search_path=pg_catalog']::text[]
    ) THEN
      RAISE EXCEPTION 'local inference function % is unsafe',function_oid;
    END IF;
    IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
       OR EXISTS (
         SELECT 1 FROM pg_proc AS procedure
         CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
         WHERE procedure.oid=function_oid AND acl.grantee=0
           AND acl.privilege_type='EXECUTE'
       ) THEN
      RAISE EXCEPTION 'local inference function % has unsafe ACL',function_oid;
    END IF;
  END LOOP;

  IF has_table_privilege(
       'brains_app','memory.v5_local_inference_event','SELECT'
     ) OR has_table_privilege(
       'brains_app','memory.v5_local_inference_event','INSERT'
     ) OR has_table_privilege(
       'brains_app','memory.v5_local_inference_event','UPDATE'
     ) OR has_table_privilege(
       'brains_app','memory.v5_local_inference_event','DELETE'
     ) OR has_table_privilege(
       'brains_app','memory.evidence_extraction_packet_v5_local','SELECT'
     ) OR has_table_privilege(
       'brains_app','memory.evidence_extraction_packet_v5_local','INSERT'
     ) OR has_table_privilege(
       'brains_app','memory.evidence_extraction_packet_v5_local','UPDATE'
     ) OR has_table_privilege(
       'brains_app','memory.evidence_extraction_packet_v5_local','DELETE'
     ) THEN
    RAISE EXCEPTION 'brains_app has direct local inference table rights';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid IN (
      'memory.v5_local_inference_event'::regclass,
      'memory.evidence_extraction_packet_v5_local'::regclass
    ) AND relrowsecurity AND relforcerowsecurity
    GROUP BY relrowsecurity,relforcerowsecurity
    HAVING count(*)=2
  ) THEN
    RAISE EXCEPTION 'local inference tables lack forced RLS';
  END IF;
  IF (
    SELECT count(*) FROM pg_trigger
    WHERE tgrelid IN (
      'memory.v5_local_inference_event'::regclass,
      'memory.evidence_extraction_packet_v5_local'::regclass
    ) AND tgname IN (
      'v5_local_inference_event_append_only_guard',
      'v5_local_packet_append_only_guard'
    ) AND tgenabled='O'
  )<>2 THEN
    RAISE EXCEPTION 'local inference append-only triggers are absent';
  END IF;
END
$security$;

BEGIN;

SELECT set_config(
  'app.user_id','fa111111-1111-4111-8111-111111111111',true
);
INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,content,
  content_sha256,recorded_at,sensitivity,status,metadata
) VALUES (
  'fa200000-0000-4000-8000-000000000001',
  'fa111111-1111-4111-8111-111111111111','user_statement',
  'public.chat_log','local-inference-owner-a','My name is Avery.',
  repeat('a',64),'2026-07-18T05:00:00Z','medium','active','{}'
);
INSERT INTO memory.evidence_intake_terminal(
  terminal_id,owner_user_id,evidence_id,selector_version,outcome,reason_code,
  evidence_content_sha256,decision_fingerprint,actor_user_id,
  invoked_by_role,details
) VALUES (
  'fa300000-0000-4000-8000-000000000001',
  'fa111111-1111-4111-8111-111111111111',
  'fa200000-0000-4000-8000-000000000001','local_inference_test_v1',
  'dispatched','eligible_dispatched',repeat('a',64),repeat('1',64),
  'fa111111-1111-4111-8111-111111111111','sage',
  '{"route":"relational_extraction"}'
);
INSERT INTO memory.evidence_extraction_job(
  job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
  evidence_content_sha256,route,intake_reason_code,status,priority
) VALUES (
  'fa400000-0000-4000-8000-000000000001',
  'fa111111-1111-4111-8111-111111111111',
  'fa200000-0000-4000-8000-000000000001',
  'fa300000-0000-4000-8000-000000000001','local_inference_test_v1',
  repeat('a',64),'relational_extraction','eligible_unprocessed','pending',0
);
INSERT INTO memory.evidence_extraction_event(
  owner_user_id,job_id,event_type,from_status,to_status,actor_type,
  actor_ref,details
) VALUES (
  'fa111111-1111-4111-8111-111111111111',
  'fa400000-0000-4000-8000-000000000001','queued',NULL,'pending',
  'system','local-inference-test','{}'
);

SELECT set_config(
  'app.user_id','fb222222-2222-4222-8222-222222222222',true
);
INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,content,
  content_sha256,recorded_at,sensitivity,status,metadata
) VALUES (
  'fb200000-0000-4000-8000-000000000001',
  'fb222222-2222-4222-8222-222222222222','user_statement',
  'public.chat_log','local-inference-owner-b','My name is Blake.',
  repeat('b',64),'2026-07-18T05:01:00Z','medium','active','{}'
);
INSERT INTO memory.evidence_intake_terminal(
  terminal_id,owner_user_id,evidence_id,selector_version,outcome,reason_code,
  evidence_content_sha256,decision_fingerprint,actor_user_id,
  invoked_by_role,details
) VALUES (
  'fb300000-0000-4000-8000-000000000001',
  'fb222222-2222-4222-8222-222222222222',
  'fb200000-0000-4000-8000-000000000001','local_inference_test_v1',
  'dispatched','eligible_dispatched',repeat('b',64),repeat('2',64),
  'fb222222-2222-4222-8222-222222222222','sage',
  '{"route":"relational_extraction"}'
);
INSERT INTO memory.evidence_extraction_job(
  job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
  evidence_content_sha256,route,intake_reason_code,status,priority
) VALUES (
  'fb400000-0000-4000-8000-000000000001',
  'fb222222-2222-4222-8222-222222222222',
  'fb200000-0000-4000-8000-000000000001',
  'fb300000-0000-4000-8000-000000000001','local_inference_test_v1',
  repeat('b',64),'relational_extraction','eligible_unprocessed','pending',0
);
INSERT INTO memory.evidence_extraction_event(
  owner_user_id,job_id,event_type,from_status,to_status,actor_type,
  actor_ref,details
) VALUES (
  'fb222222-2222-4222-8222-222222222222',
  'fb400000-0000-4000-8000-000000000001','queued',NULL,'pending',
  'system','local-inference-test','{}'
);

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','fa111111-1111-4111-8111-111111111111',true
);

SELECT * FROM memory.claim_owner_v5_local_inference_job_v1(
  'fa500000-0000-4000-8000-000000000001',
  'fa500000-0000-4000-8000-000000000002',
  'fa400000-0000-4000-8000-000000000001',repeat('a',64),
  'relational_extraction','local-inference-test',300,1,
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),86400,12,3
) \gset claim_

SELECT
  1/((:'claim_job_id'='fa400000-0000-4000-8000-000000000001')::integer),
  1/((:'claim_status'='processing')::integer),
  1/((:'claim_control_outcome'='reserved')::integer),
  1/((:'claim_apply_outcome'='applied')::integer),
  1/((:'claim_evidence_content_sha256'=repeat('a',64))::integer);

SELECT
  1/((job_id=:'claim_job_id'::uuid)::integer),
  1/((reservation_event_id=:'claim_reservation_event_id'::uuid)::integer),
  1/((apply_outcome='replayed')::integer)
FROM memory.claim_owner_v5_local_inference_job_v1(
  'fa500000-0000-4000-8000-000000000001',
  'fa500000-0000-4000-8000-000000000002',
  'fa400000-0000-4000-8000-000000000001',repeat('a',64),
  'relational_extraction','local-inference-test',300,1,
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),86400,12,3
);

SELECT
  1/((job_id IS NULL)::integer),
  1/((control_outcome='quota_exhausted')::integer)
FROM memory.claim_owner_v5_local_inference_job_v1(
  'fa500000-0000-4000-8000-000000000011',
  'fa500000-0000-4000-8000-000000000012',
  'fa400000-0000-4000-8000-000000000001',repeat('a',64),
  'relational_extraction','local-inference-test',300,1,
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),86400,1,3
);

SELECT jsonb_build_object(
  'contract_version','memory_v1_relational_extraction_v5',
  'predicate_registry_version','memory_predicate_registry_v5',
  'source_envelope',jsonb_build_object(
    'job_id',:'claim_job_id',
    'source_system',:'claim_evidence_source_system',
    'source_external_id',:'claim_evidence_external_id',
    'source_sha256',:'claim_evidence_content_sha256',
    'source_recorded_at',:'claim_evidence_recorded_at'
  ),
  'entity_mentions',jsonb_build_array(jsonb_build_object(
    'entity_ref','e01','entity_type','self','mention_kind','self_reference'
  )),
  'observations',jsonb_build_array(jsonb_build_object(
    'observation_ref','o01','subject_entity_ref','e01',
    'predicate','identity.name','projection_class','direct_claim',
    'object',jsonb_build_object('kind','literal','datatype','text','value','Avery')
  )),
  'comparison_hints','[]'::jsonb,'deferrals','[]'::jsonb,
  'packet_findings','[]'::jsonb
) AS packet \gset normalized_

SELECT * FROM memory.persist_owner_v5_local_packet_v1(
  'fa600000-0000-4000-8000-000000000001',
  'fa600000-0000-4000-8000-000000000002',
  :'claim_job_id',:'claim_lease_token','local-inference-test',
  :'claim_evidence_content_sha256','v1',repeat('3',64),repeat('4',64),
  repeat('5',64),repeat('6',64),repeat('7',64),repeat('8',64),
  :'normalized_packet'::jsonb,false,0
) \gset persisted_

SELECT
  1/((:'persisted_status'='review_required')::integer),
  1/((:'persisted_apply_outcome'='applied')::integer);

SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.persist_owner_v5_local_packet_v1(
  'fa600000-0000-4000-8000-000000000001',
  'fa600000-0000-4000-8000-000000000002',
  :'claim_job_id',:'claim_lease_token','local-inference-test',
  :'claim_evidence_content_sha256','v1',repeat('3',64),repeat('4',64),
  repeat('5',64),repeat('6',64),repeat('7',64),repeat('8',64),
  :'normalized_packet'::jsonb,false,0
);

SELECT * FROM memory.complete_owner_v5_local_inference_v1(
  'fa700000-0000-4000-8000-000000000001',
  :'claim_reservation_event_id',
  'fa500000-0000-4000-8000-000000000002',:'claim_job_id',
  'accepted',0,NULL,repeat('7',64),repeat('8',64),
  :'persisted_packet_storage_sha256'
) \gset completed_

SELECT
  1/((:'completed_outcome'='accepted')::integer),
  1/((:'completed_external_model_calls'='0')::integer),
  1/((:'completed_apply_outcome'='applied')::integer);
SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.complete_owner_v5_local_inference_v1(
  'fa700000-0000-4000-8000-000000000001',
  :'claim_reservation_event_id',
  'fa500000-0000-4000-8000-000000000002',:'claim_job_id',
  'accepted',0,NULL,repeat('7',64),repeat('8',64),
  :'persisted_packet_storage_sha256'
);

SELECT set_config(
  'test.local_reservation_event_id',:'claim_reservation_event_id',true
);
SELECT set_config(
  'test.local_run_id','fa500000-0000-4000-8000-000000000002',true
);
SELECT set_config('test.local_job_id',:'claim_job_id',true);
SELECT set_config(
  'test.local_packet_storage_sha256',
  :'persisted_packet_storage_sha256',true
);

SELECT
  1/((status='review_required')::integer),
  1/((apply_outcome='replayed')::integer)
FROM memory.claim_owner_v5_local_inference_job_v1(
  'fa500000-0000-4000-8000-000000000001',
  'fa500000-0000-4000-8000-000000000002',
  'fa400000-0000-4000-8000-000000000001',repeat('a',64),
  'relational_extraction','local-inference-test',300,1,
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),86400,12,3
);

SELECT set_config(
  'app.user_id','fb222222-2222-4222-8222-222222222222',true
);
DO $cross_owner$
BEGIN
  PERFORM * FROM memory.complete_owner_v5_local_inference_v1(
    'fb700000-0000-4000-8000-000000000001',
    current_setting('test.local_reservation_event_id')::uuid,
    current_setting('test.local_run_id')::uuid,
    current_setting('test.local_job_id')::uuid,
    'accepted',0,NULL,repeat('7',64),repeat('8',64),
    current_setting('test.local_packet_storage_sha256')
  );
  RAISE EXCEPTION 'cross-owner completion unexpectedly succeeded';
EXCEPTION WHEN check_violation THEN NULL;
END
$cross_owner$;

SELECT * FROM memory.claim_owner_v5_local_inference_job_v1(
  'fb500000-0000-4000-8000-000000000001',
  'fb500000-0000-4000-8000-000000000002',
  'fb400000-0000-4000-8000-000000000001',repeat('b',64),
  'relational_extraction','local-inference-test',300,1,
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),86400,12,1
) \gset claim_b_
SELECT 1/((:'claim_b_evidence_content_sha256'=repeat('b',64))::integer);

SELECT * FROM memory.complete_owner_v5_local_inference_v1(
  'fb700000-0000-4000-8000-000000000011',
  :'claim_b_reservation_event_id',
  'fb500000-0000-4000-8000-000000000002',:'claim_b_job_id',
  'rejected',0,'synthetic_local_rejected',NULL,NULL,NULL
) \gset rejected_
SELECT 1/((:'rejected_outcome'='rejected')::integer);

SELECT
  1/((job_id IS NULL)::integer),
  1/((control_outcome='circuit_open')::integer),
  1/((consecutive_rejections=1)::integer)
FROM memory.claim_owner_v5_local_inference_job_v1(
  'fb500000-0000-4000-8000-000000000011',
  'fb500000-0000-4000-8000-000000000012',
  'fb400000-0000-4000-8000-000000000001',repeat('b',64),
  'relational_extraction','local-inference-test',300,1,
  'local_llama_cpp','v1',repeat('3',64),repeat('4',64),repeat('5',64),
  repeat('6',64),86400,12,1
);

DO $direct_write$
BEGIN
  BEGIN
    UPDATE memory.v5_local_inference_event SET outcome='accepted';
    RAISE EXCEPTION 'direct local ledger update unexpectedly succeeded';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    DELETE FROM memory.evidence_extraction_packet_v5_local;
    RAISE EXCEPTION 'direct local packet deletion unexpectedly succeeded';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
END
$direct_write$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_inference: PASS' AS result;
