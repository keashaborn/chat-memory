\set ON_ERROR_STOP on

SELECT set_config('test.target_job',:'target_job',false);
SELECT set_config('test.reservation_event',:'reservation_event',false);
SELECT set_config('test.content_sha256',:'content_sha256',false);

DO $catalog$
DECLARE
  recovery regprocedure :=
    'memory.recover_owner_v5_local_orphan_v1(uuid,uuid,uuid,uuid,text)'::regprocedure;
BEGIN
  IF to_regrole('memory_v5_local_inference_maintainer') IS NULL
     OR (SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                OR rolinherit OR rolbypassrls
         FROM pg_roles
         WHERE rolname='memory_v5_local_inference_maintainer')
     OR (SELECT rolname<>'memory_v5_local_inference_maintainer'
         FROM pg_proc JOIN pg_roles ON pg_roles.oid=pg_proc.proowner
         WHERE pg_proc.oid=recovery)
     OR NOT has_function_privilege('brains_app',recovery,'EXECUTE')
     OR has_table_privilege(
       'brains_app','memory.v5_local_inference_event','INSERT'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_extraction_job','UPDATE'
     ) THEN
    RAISE EXCEPTION 'V5 local orphan recovery ACL is unsafe';
  END IF;
END
$catalog$;

BEGIN;
SET LOCAL statement_timeout='30s';
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'other_owner',true);

DO $cross_owner$
BEGIN
  PERFORM * FROM memory.recover_owner_v5_local_orphan_v1(
    '81000000-0000-4000-8000-000000000001',
    '81000000-0000-4000-8000-000000000002',
    current_setting('test.target_job')::uuid,
    current_setting('test.reservation_event')::uuid,
    current_setting('test.content_sha256')
  );
  RAISE EXCEPTION 'cross-owner orphan recovery unexpectedly succeeded';
EXCEPTION WHEN check_violation THEN NULL;
END
$cross_owner$;

SELECT set_config('app.user_id',:'target_owner',true);
SELECT * FROM memory.recover_owner_v5_local_orphan_v1(
  '82000000-0000-4000-8000-000000000001',
  '82000000-0000-4000-8000-000000000002',
  :'target_job'::uuid,:'reservation_event'::uuid,
  :'content_sha256'
) \gset applied_

SELECT 1/((:'applied_job_id'=:'target_job')::integer),
       1/((:'applied_status'='skipped')::integer),
       1/((:'applied_accounted_local_model_calls'='1')::integer),
       1/((:'applied_apply_outcome'='applied')::integer);

SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_job
WHERE owner_user_id=:'target_owner'::uuid
  AND job_id=:'target_job'::uuid
  AND status='skipped'
  AND lease_token IS NULL AND lease_expires_at IS NULL
  AND worker_id IS NULL
  AND last_error='local_worker_abandoned'
  AND result#>>'{final,reason_code}'='local_worker_abandoned';

SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_event
WHERE owner_user_id=:'target_owner'::uuid
  AND operation_id='82000000-0000-4000-8000-000000000001'::uuid
  AND job_id=:'target_job'::uuid
  AND event_type='skipped'
  AND from_status='processing' AND to_status='skipped'
  AND actor_ref='memory_v1_v5_local_orphan_recovery_v1'
  AND details->>'source_prose_copied'='false'
  AND details->>'local_model_calls_accounted'='1'
  AND details->>'claims'='0'
  AND details->>'qdrant'='0'
  AND details->>'prompt_influence'='0';

RESET SESSION AUTHORIZATION;
SELECT 1/((count(*)=1)::integer)
FROM memory.v5_local_inference_event AS completion
JOIN memory.v5_local_inference_event AS reservation
  ON reservation.owner_user_id=completion.owner_user_id
 AND reservation.event_id=completion.reservation_event_id
WHERE completion.owner_user_id=:'target_owner'::uuid
  AND completion.operation_id='82000000-0000-4000-8000-000000000002'::uuid
  AND completion.job_id=:'target_job'::uuid
  AND completion.reservation_event_id=:'reservation_event'::uuid
  AND completion.action='completed' AND completion.outcome='rejected'
  AND completion.rejection_code='local_worker_abandoned'
  AND completion.local_model_calls=1 AND completion.external_model_calls=0
  AND completion.created_at=reservation.created_at+interval '1 microsecond';

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'target_owner',true);
SELECT * FROM memory.recover_owner_v5_local_orphan_v1(
  '82000000-0000-4000-8000-000000000001',
  '82000000-0000-4000-8000-000000000002',
  :'target_job'::uuid,:'reservation_event'::uuid,
  :'content_sha256'
) \gset replay_
SELECT 1/((:'replay_apply_outcome'='replayed')::integer),
       1/((:'replay_recovery_event_id'=:'applied_recovery_event_id')::integer),
       1/((:'replay_completion_event_id'=:'applied_completion_event_id')::integer);

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_orphan_recovery: PASS' AS result;
