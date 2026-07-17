\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid regprocedure;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_extraction_maintainer'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'memory_v5_extraction_maintainer role is unsafe';
  END IF;

  FOREACH function_oid IN ARRAY ARRAY[
    'memory.apply_owner_project_thread_binding_v5(uuid,uuid,uuid,text,text)'::regprocedure,
    'memory.read_owner_evidence_extraction_context_v5(uuid,uuid,text,text)'::regprocedure,
    'memory.persist_owner_evidence_extraction_packet_v5(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,jsonb,boolean,integer,uuid)'::regprocedure
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_proc
      WHERE oid=function_oid
        AND prosecdef
        AND proowner='memory_v5_extraction_maintainer'::regrole
        AND proconfig=ARRAY['search_path=pg_catalog']::text[]
    ) THEN
      RAISE EXCEPTION 'V5 extraction function % is unsafe',function_oid;
    END IF;
    IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
       OR EXISTS (
         SELECT 1
         FROM pg_proc AS procedure
         CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
         WHERE procedure.oid=function_oid
           AND acl.grantee=0
           AND acl.privilege_type='EXECUTE'
       ) THEN
      RAISE EXCEPTION 'V5 extraction function % has unsafe ACL',function_oid;
    END IF;
  END LOOP;

  IF has_table_privilege(
       'brains_app','memory.project_thread_binding_event','INSERT'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_extraction_packet_v5','INSERT'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_extraction_packet_v5','UPDATE'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_extraction_packet_v5','DELETE'
     ) THEN
    RAISE EXCEPTION 'brains_app has direct V5 extraction mutation rights';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid IN (
      'memory.project_thread_binding_event'::regclass,
      'memory.evidence_extraction_packet_v5'::regclass
    )
      AND relrowsecurity
      AND relforcerowsecurity
    GROUP BY relrowsecurity,relforcerowsecurity
    HAVING count(*)=2
  ) THEN
    RAISE EXCEPTION 'V5 extraction tables lack forced RLS';
  END IF;
END
$security$;

BEGIN;

CREATE FUNCTION pg_temp.assert_v5_call_denied(p_sql text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    EXECUTE p_sql;
  EXCEPTION
    WHEN check_violation OR insufficient_privilege OR foreign_key_violation
      OR invalid_parameter_value THEN denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'unsafe V5 extraction call was accepted: %',p_sql;
  END IF;
END
$function$;

INSERT INTO public.threads(
  id,user_id,title,created_at,updated_at,archived,owner_user_id
) VALUES
  (
    'fa000000-0000-4000-8000-000000000001',
    'fa111111-1111-4111-8111-111111111111',
    'V5 owner A fixture',clock_timestamp(),clock_timestamp(),false,
    'fa111111-1111-4111-8111-111111111111'
  ),
  (
    'fb000000-0000-4000-8000-000000000001',
    'fb222222-2222-4222-8222-222222222222',
    'V5 owner B fixture',clock_timestamp(),clock_timestamp(),false,
    'fb222222-2222-4222-8222-222222222222'
  );

INSERT INTO memory.project_space(
  project_id,owner_user_id,project_key,display_name,metadata
) VALUES
  (
    'fa000000-0000-4000-8000-000000000002',
    'fa111111-1111-4111-8111-111111111111',
    'verbal-sage','Verbal Sage','{"test":true}'::jsonb
  ),
  (
    'fb000000-0000-4000-8000-000000000002',
    'fb222222-2222-4222-8222-222222222222',
    'other-project','Other Project','{"test":true}'::jsonb
  );

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','fa111111-1111-4111-8111-111111111111',true
);

SELECT
  binding_event_id,
  project_key,
  action,
  apply_outcome
FROM memory.apply_owner_project_thread_binding_v5(
  'fa100000-0000-4000-8000-000000000001',
  'fa000000-0000-4000-8000-000000000001',
  'fa000000-0000-4000-8000-000000000002',
  'bind','explicit_project_thread_binding'
)
\gset binding_
SELECT
  1/((:'binding_project_key'='verbal-sage')::integer),
  1/((:'binding_action'='bind')::integer),
  1/((:'binding_apply_outcome'='applied')::integer);

SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.apply_owner_project_thread_binding_v5(
  'fa100000-0000-4000-8000-000000000001',
  'fa000000-0000-4000-8000-000000000001',
  'fa000000-0000-4000-8000-000000000002',
  'bind','explicit_project_thread_binding'
);
SELECT 1/((count(*)=1)::integer)
FROM memory.project_thread_binding_event;
SELECT 1/((count(*)=1)::integer)
FROM memory.current_project_thread_binding_v5
WHERE thread_id='fa000000-0000-4000-8000-000000000001'
  AND project_key='verbal-sage';

SELECT evidence_id,content_sha256
FROM memory.record_owner_evidence_v1(
  'user_statement','public.chat_log',
  'fa200000-0000-4000-8000-000000000001',
  'Verbal Sage must keep owner boundaries fail-closed.',
  '2026-07-17T12:00:00Z',1,1,
  'public.chat_log:thread:fa000000-0000-4000-8000-000000000001',
  'medium',
  '{
    "capture_version":"test",
    "thread_id":"fa000000-0000-4000-8000-000000000001",
    "semantic_processing":"pending"
  }'::jsonb
)
\gset evidence_

SELECT job_id
FROM memory.enqueue_owner_evidence_extraction_v1(
  :'evidence_evidence_id',
  '20260717_v5_bounded_test',
  :'evidence_content_sha256',
  'relational_extraction','eligible_unprocessed'
)
\gset queue_

SELECT *
FROM memory.claim_owner_evidence_extraction_job_v1(
  'fa300000-0000-4000-8000-000000000001',
  'relational_extraction','v5-bounded-test',300,1
)
\gset claim_

SELECT
  1/((thread_id='fa000000-0000-4000-8000-000000000001')::integer),
  1/((project_id='fa000000-0000-4000-8000-000000000002')::integer),
  1/((project_key='verbal-sage')::integer),
  1/((binding_event_id=:'binding_binding_event_id')::integer)
FROM memory.read_owner_evidence_extraction_context_v5(
  :'claim_job_id',:'claim_lease_token','v5-bounded-test',
  :'claim_evidence_content_sha256'
);

SELECT jsonb_build_object(
  'contract_version','memory_v1_relational_extraction_v5',
  'source_envelope',jsonb_build_object(
    'job_id',:'claim_job_id',
    'source_system','public.chat_log',
    'source_external_id','fa200000-0000-4000-8000-000000000001',
    'source_sha256',:'claim_evidence_content_sha256',
    'source_recorded_at',:'claim_evidence_recorded_at'
  ),
  'predicate_registry_version','memory_predicate_registry_v5',
  'entity_mentions',jsonb_build_array(jsonb_build_object(
    'entity_ref','e01','entity_type','project','mention_kind','named',
    'name_text','Verbal Sage','relationship_role',NULL,
    'source_spans',jsonb_build_array(jsonb_build_object(
      'start',0,'end',11,'span_sha256',
      encode(public.digest(convert_to('Verbal Sage','UTF8'),'sha256'),'hex')
    )),
    'extraction_confidence',1.0,
    'reason_codes',jsonb_build_array('explicit_project_name')
  )),
  'observations',jsonb_build_array(jsonb_build_object(
    'observation_ref','o01','subject_entity_ref','e01',
    'predicate','project.requirement','predicate_registry_status','governed',
    'object',jsonb_build_object(
      'kind','literal','datatype','text',
      'value','keep owner boundaries fail-closed','unit',NULL,
      'approximate',false
    ),
    'polarity','affirmed','modality','asserted',
    'projection_class','project_knowledge',
    'surface_policy','exact_project_scope_only',
    'temporal',jsonb_build_object(
      'semantic','observation_time','shape','instant','basis','instant',
      'source_form','implicit_source_time','certainty','exact',
      'precision','minute','instant',:'claim_evidence_recorded_at',
      'calendar_range',NULL,'instant_range',NULL,
      'relative_offset',NULL,'recurrence',NULL,
      'anchored_to_source_time',true,'reason_codes',jsonb_build_array(
        'observation_time_uses_source_timestamp'
      ),
      'normalization_policy_version','memory_temporal_normalization_v5'
    ),
    'project_scope',jsonb_build_object(
      'state','resolved','project_key','verbal-sage',
      'binding_source','trusted_thread_binding'
    ),
    'sensitivity','medium','extraction_confidence',0.99,
    'source_spans',jsonb_build_array(jsonb_build_object(
      'start',0,'end',51,'span_sha256',:'claim_evidence_content_sha256'
    )),
    'reason_codes',jsonb_build_array('explicit_project_requirement')
  )),
  'comparison_hints','[]'::jsonb,
  'deferrals','[]'::jsonb,
  'packet_findings',jsonb_build_array('synthetic_project_contract_test')
) AS packet
\gset

SELECT jsonb_set(
  :'packet'::jsonb,
  '{entity_mentions,0,name_text}',
  '"Other Project"'::jsonb
) AS mismatched_packet
\gset
SELECT pg_temp.assert_v5_call_denied(format(
  $sql$
    SELECT * FROM memory.persist_owner_evidence_extraction_packet_v5(
      'fa400000-0000-4000-8000-000000000098',
      'fa400000-0000-4000-8000-000000000099',%L,%L,
      'v5-bounded-test',%L,'synthetic_fixture','v1',%L,%L,%L,
      %L::jsonb,true,0,%L
    )
  $sql$,
  :'claim_job_id',:'claim_lease_token',:'claim_evidence_content_sha256',
  repeat('c',64),repeat('b',64),repeat('d',64),
  :'mismatched_packet',:'binding_binding_event_id'
));

SELECT
  packet_id,
  validator_packet_sha256,
  packet_storage_sha256,
  status,
  apply_outcome
FROM memory.persist_owner_evidence_extraction_packet_v5(
  'fa400000-0000-4000-8000-000000000001',
  'fa400000-0000-4000-8000-000000000002',
  :'claim_job_id',:'claim_lease_token','v5-bounded-test',
  :'claim_evidence_content_sha256',
  'synthetic_fixture','v1',repeat('c',64),repeat('b',64),repeat('a',64),
  :'packet'::jsonb,true,0,:'binding_binding_event_id'
)
\gset persisted_
SELECT
  1/((:'persisted_status'='review_required')::integer),
  1/((:'persisted_apply_outcome'='applied')::integer);

SELECT
  1/((validator_packet_sha256=repeat('a',64))::integer),
  1/((packet_storage_sha256=:'persisted_packet_storage_sha256')::integer),
  1/((apply_outcome='replayed')::integer)
FROM memory.persist_owner_evidence_extraction_packet_v5(
  'fa400000-0000-4000-8000-000000000001',
  'fa400000-0000-4000-8000-000000000002',
  :'claim_job_id',:'claim_lease_token','v5-bounded-test',
  :'claim_evidence_content_sha256',
  'synthetic_fixture','v1',repeat('c',64),repeat('b',64),repeat('a',64),
  :'packet'::jsonb,true,0,:'binding_binding_event_id'
);

SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_packet_v5
WHERE packet_id=:'persisted_packet_id'
  AND entity_mention_count=1
  AND observation_count=1
  AND comparison_hint_count=0
  AND deferral_count=0;
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_job
WHERE job_id=:'claim_job_id'
  AND status='review_required'
  AND result#>>'{final,sha256}'=:'persisted_packet_storage_sha256'
  AND result#>>'{final,payload,validator_packet_sha256}'=repeat('a',64)
  AND result#>>'{final,payload,write_counts,qdrant}'='0';
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_event
WHERE job_id=:'claim_job_id'
  AND operation_id='fa400000-0000-4000-8000-000000000001'
  AND details->>'packet_storage_sha256'
      =:'persisted_packet_storage_sha256';

SELECT pg_temp.assert_v5_call_denied(
  'UPDATE memory.evidence_extraction_packet_v5 '
  'SET manual_review_required=false'
);
SELECT pg_temp.assert_v5_call_denied(
  'DELETE FROM memory.project_thread_binding_event'
);

SELECT set_config(
  'app.user_id','fb222222-2222-4222-8222-222222222222',true
);
SELECT 1/((count(*)=0)::integer)
FROM memory.evidence_extraction_packet_v5;
SELECT 1/((count(*)=0)::integer)
FROM memory.project_thread_binding_event;
SELECT pg_temp.assert_v5_call_denied(format(
  $sql$
    SELECT * FROM memory.apply_owner_project_thread_binding_v5(
      'fb100000-0000-4000-8000-000000000001',%L,%L,
      'bind','cross_owner_attempt'
    )
  $sql$,
  'fa000000-0000-4000-8000-000000000001',
  'fa000000-0000-4000-8000-000000000002'
));
SELECT pg_temp.assert_v5_call_denied(format(
  $sql$
    SELECT * FROM memory.persist_owner_evidence_extraction_packet_v5(
      'fb400000-0000-4000-8000-000000000001',%L,%L,%L,
      'v5-bounded-test',%L,'synthetic_fixture','v1',%L,%L,%L,
      %L::jsonb,true,0,%L
    )
  $sql$,
  :'persisted_packet_id',:'claim_job_id',:'claim_lease_token',
  :'claim_evidence_content_sha256',repeat('c',64),repeat('b',64),
  repeat('a',64),:'packet',:'binding_binding_event_id'
));

ROLLBACK;

SELECT 'memory_v1_v5_bounded_extraction: PASS' AS result;
