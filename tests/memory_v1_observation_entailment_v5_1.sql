\set ON_ERROR_STOP on

BEGIN;

SELECT jsonb_build_array(jsonb_build_object(
  'start',0,
  'end',242,
  'span_sha256',encode(public.digest(
    convert_to(substring(evidence.content FROM 1 FOR 242),'UTF8'),
    'sha256'
  ),'hex')
))::text AS source_spans
FROM memory.evidence AS evidence
WHERE evidence.owner_user_id
        ='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
  AND evidence.evidence_id
        ='fca9e5dc-83c2-4456-8db8-1fe6102eb74d'::uuid
\gset occupation_

SELECT source_spans::text AS source_spans
FROM memory.observation
WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
  AND observation_id='93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid
\gset correction_

SET SESSION AUTHORIZATION brains_app;

DO $missing_actor$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_observation_entailment_v5(
      '9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid,
      'deferred'::memory.observation_entailment_decision_v5,
      'source_contradicts_predicate',
      jsonb_build_array(jsonb_build_object(
        'start',0,'end',1,'span_sha256',repeat('a',64)
      )),
      'system','memory_v1_observation_entailment_v5_1_test'
    );
    RAISE EXCEPTION 'missing-actor entailment preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$missing_actor$;

SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

DO $direct_table_denial$
BEGIN
  BEGIN
    PERFORM 1 FROM memory.observation_entailment_v5 LIMIT 1;
    RAISE EXCEPTION 'brains_app directly read observation entailment';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
  BEGIN
    INSERT INTO memory.observation_entailment_v5(
      owner_user_id,observation_id,observation_sha256,
      evidence_id,evidence_content_sha256,policy_version,decision,
      reason_code,source_spans,authorization_manifest_sha256,
      assessor_type,assessor_ref,invoked_by_session
    ) VALUES (
      '1240822d-ac9a-4096-95aa-e2b24d36ef50',
      '9bf1e6b2-1840-4524-98dc-142567ebe013',repeat('a',64),
      'fca9e5dc-83c2-4456-8db8-1fe6102eb74d',repeat('b',64),
      'memory_v1_predicate_entailment_v5_1','deferred',
      'source_contradicts_predicate',
      jsonb_build_array(jsonb_build_object(
        'start',0,'end',1,'span_sha256',repeat('c',64)
      )),
      repeat('d',64),'system','direct_write','brains_app'
    );
    RAISE EXCEPTION 'brains_app directly wrote observation entailment';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$direct_table_denial$;

DO $tampered_span$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_observation_entailment_v5(
      '9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid,
      'deferred'::memory.observation_entailment_decision_v5,
      'source_contradicts_predicate',
      jsonb_build_array(jsonb_build_object(
        'start',0,'end',242,'span_sha256',repeat('a',64)
      )),
      'system','memory_v1_observation_entailment_v5_1_test'
    );
    RAISE EXCEPTION 'tampered entailment span unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '23514' THEN
    NULL;
  END;
END
$tampered_span$;

DO $insufficient_span$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_observation_entailment_v5(
      '9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid,
      'deferred'::memory.observation_entailment_decision_v5,
      'source_contradicts_predicate',
      jsonb_build_array(jsonb_build_object(
        'start',0,
        'end',1,
        'span_sha256','a83dd0ccbffe39d071cc317ddf6e97f5c6b1c87af91919271f9fa140b0508c6c'
      )),
      'system','memory_v1_observation_entailment_v5_1_test'
    );
    RAISE EXCEPTION 'insufficient entailment span unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '23514' THEN
    NULL;
  END;
END
$insufficient_span$;

SELECT * FROM memory.preflight_observation_entailment_v5(
  '9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid,
  'deferred'::memory.observation_entailment_decision_v5,
  'source_contradicts_predicate',
  :'occupation_source_spans'::jsonb,
  'system','memory_v1_observation_entailment_v5_1_test'
) \gset occupation_preflight_

SELECT 1 / ((:'occupation_preflight_observation_sha256'
  ='8a6ebf42194c1cb1f73edb1db3e1339db5adcc8d455ff0f4c77c1ddabc29501d')::integer);
SELECT 1 / ((:'occupation_preflight_evidence_content_sha256'
  ='d41de5228017def6a31b3286cfb4033b8c26a251e73b3c16af67990bfb276de6')::integer);
SELECT 1 / ((:'occupation_preflight_policy_version'
  ='memory_v1_predicate_entailment_v5_1')::integer);
SELECT 1 / ((:'occupation_preflight_decision'='deferred')::integer);

SELECT * FROM memory.record_observation_entailment_v5(
  '41000000-0000-4000-8000-000000000001'::uuid,
  '9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid,
  'deferred'::memory.observation_entailment_decision_v5,
  'source_contradicts_predicate',
  :'occupation_source_spans'::jsonb,
  'system','memory_v1_observation_entailment_v5_1_test',
  :'occupation_preflight_authorization_manifest_sha256'
) \gset occupation_apply_

SELECT 1 / ((:'occupation_apply_outcome'='applied')::integer);
SELECT 1 / ((:'occupation_apply_rows_written'::integer=2)::integer);

SELECT * FROM memory.record_observation_entailment_v5(
  '41000000-0000-4000-8000-000000000001'::uuid,
  '9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid,
  'deferred'::memory.observation_entailment_decision_v5,
  'source_contradicts_predicate',
  :'occupation_source_spans'::jsonb,
  'system','memory_v1_observation_entailment_v5_1_test',
  :'occupation_preflight_authorization_manifest_sha256'
) \gset occupation_replay_

SELECT 1 / ((:'occupation_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'occupation_replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'occupation_replay_decision_id'
  =:'occupation_apply_decision_id')::integer);
DO $different_request_rejected$
BEGIN
  BEGIN
    PERFORM * FROM memory.record_observation_entailment_v5(
      '41000000-0000-4000-8000-000000000003'::uuid,
      '9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid,
      'deferred'::memory.observation_entailment_decision_v5,
      'source_contradicts_predicate',
      jsonb_build_array(jsonb_build_object(
        'start',0,
        'end',242,
        'span_sha256','1b0aabe1bee3fe85ca6c77ffbd9517aa4a38c3a079aa6a6cfe57927aecd59bf8'
      )),
      'system','memory_v1_observation_entailment_v5_1_test',
      '6dcb2b662589b1cb6bcb1582e9c3b939f8d678411132fb870c8e57449edc9f9b'
    );
    RAISE EXCEPTION 'different request_id unexpectedly replayed a decision';
  EXCEPTION WHEN SQLSTATE '23514' THEN
    NULL;
  END;
END
$different_request_rejected$;
SELECT 1 / ((NOT memory.observation_entailment_allows_projection_v5(
  '9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid,
  '8a6ebf42194c1cb1f73edb1db3e1339db5adcc8d455ff0f4c77c1ddabc29501d'
))::integer);

RESET SESSION AUTHORIZATION;
-- Transaction-local test membership reproduces the projection stage API's
-- session_user=brains_app/current_user=memory_v5_writer execution context.
GRANT memory_v5_writer TO brains_app;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SET ROLE memory_v5_writer;
DO $projection_blocked$
BEGIN
  BEGIN
    INSERT INTO memory.projection_plan_observation(
      owner_user_id,plan_id,projection_ref,
      observation_id,observation_sha256,stance
    ) VALUES (
      '1240822d-ac9a-4096-95aa-e2b24d36ef50',
      '33000000-0000-4000-8000-000000000001','p01',
      '9bf1e6b2-1840-4524-98dc-142567ebe013',
      '8a6ebf42194c1cb1f73edb1db3e1339db5adcc8d455ff0f4c77c1ddabc29501d',
      'supports'
    );
    RAISE EXCEPTION 'deferred observation entered projection';
  EXCEPTION WHEN SQLSTATE '23514' THEN
    NULL;
  END;
END
$projection_blocked$;
RESET ROLE;

SELECT * FROM memory.preflight_observation_entailment_v5(
  '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid,
  'accepted'::memory.observation_entailment_decision_v5,
  'predicate_entailment_v5_1_accepted',
  :'correction_source_spans'::jsonb,
  'system','memory_v1_observation_entailment_v5_1_test'
) \gset correction_preflight_

SELECT * FROM memory.record_observation_entailment_v5(
  '41000000-0000-4000-8000-000000000002'::uuid,
  '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid,
  'accepted'::memory.observation_entailment_decision_v5,
  'predicate_entailment_v5_1_accepted',
  :'correction_source_spans'::jsonb,
  'system','memory_v1_observation_entailment_v5_1_test',
  :'correction_preflight_authorization_manifest_sha256'
) \gset correction_apply_

SELECT 1 / ((:'correction_apply_outcome'='applied')::integer);
SELECT 1 / ((:'correction_apply_rows_written'::integer=2)::integer);
SELECT 1 / ((memory.observation_entailment_allows_projection_v5(
  '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid,
  '28b4cb5db9082ca6b718cb7588a1740b1d9dbb6ddd5486d90758facfe00fb675'
))::integer);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT 1 / ((NOT memory.observation_entailment_allows_projection_v5(
  '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid,
  '28b4cb5db9082ca6b718cb7588a1740b1d9dbb6ddd5486d90758facfe00fb675'
))::integer);
DO $cross_owner$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_observation_entailment_v5(
      '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid,
      'accepted'::memory.observation_entailment_decision_v5,
      'predicate_entailment_v5_1_accepted',
      jsonb_build_array(jsonb_build_object(
        'start',66,
        'end',70,
        'span_sha256','016526330aaf250542e5acc9103d9f663a8a5bb00d1b8607a1b170b6d93d6401'
      )),
      'system','memory_v1_observation_entailment_v5_1_test'
    );
    RAISE EXCEPTION 'cross-owner entailment preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
SELECT 1 / (((SELECT count(*) FROM memory.observation_entailment_v5)=2)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.relational_operation_request
  WHERE request_id IN (
    '41000000-0000-4000-8000-000000000001'::uuid,
    '41000000-0000-4000-8000-000000000002'::uuid
  )
    AND operation='record_observation_entailment_v5'
)=2)::integer);

ROLLBACK;
RESET SESSION AUTHORIZATION;

SELECT 1 / (((SELECT count(*) FROM memory.observation_entailment_v5)=0)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.relational_operation_request
  WHERE request_id IN (
    '41000000-0000-4000-8000-000000000001'::uuid,
    '41000000-0000-4000-8000-000000000002'::uuid
  )
)=0)::integer);
SELECT 1 / ((EXISTS (
  SELECT 1 FROM pg_trigger
  WHERE tgrelid='memory.projection_plan_observation'::regclass
    AND tgname='projection_plan_observation_entailment_guard'
    AND NOT tgisinternal
))::integer);

SELECT 'memory_v1_observation_entailment_v5_1: PASS' AS result;
