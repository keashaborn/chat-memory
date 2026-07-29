\set ON_ERROR_STOP on

SELECT set_config('test.source_job_id', :'source_job_id', false);
SELECT set_config('test.content_sha256', :'content_sha256', false);

DO $catalog$
DECLARE
  planner regprocedure :=
    'memory.plan_owner_context_generation_retry_v1(uuid)'::regprocedure;
  enqueue regprocedure :=
    'memory.enqueue_owner_context_generation_retry_v1(uuid,uuid,uuid,uuid,text,text)'::regprocedure;
BEGIN
  IF (SELECT rolname <> 'memory_v5_local_reextract_maintainer'
      FROM pg_proc
      JOIN pg_roles ON pg_roles.oid = pg_proc.proowner
      WHERE pg_proc.oid = planner)
     OR (SELECT rolname <> 'memory_v5_local_reextract_maintainer'
         FROM pg_proc
         JOIN pg_roles ON pg_roles.oid = pg_proc.proowner
         WHERE pg_proc.oid = enqueue)
     OR NOT has_function_privilege('brains_app', planner, 'EXECUTE')
     OR NOT has_function_privilege('brains_app', enqueue, 'EXECUTE')
     OR has_table_privilege(
       'brains_app',
       'memory.evidence_extraction_job',
       'INSERT'
     ) THEN
    RAISE EXCEPTION 'context generation retry ACL is unsafe';
  END IF;
END
$catalog$;

BEGIN;
SET LOCAL statement_timeout = '30s';
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'target_owner', true);

SELECT 1 / ((count(*) = 22)::integer)
FROM memory.plan_owner_context_generation_retry_v1(NULL);

SELECT 1 / ((count(*) = 1)::integer)
FROM memory.plan_owner_context_generation_retry_v1(
  :'source_job_id'::uuid
)
WHERE evidence_id = :'target_evidence'::uuid
  AND evidence_content_sha256 = :'content_sha256'
  AND source_attempts = 1
  AND source_selector_version = '20260729_v4_contextual_resplit'
  AND splitter_version =
        'memory_v1_contextual_span_splitter_20260728_v3'
  AND reason_code = 'context_generation_overlap_corrected'
  AND next_selector_version =
        '20260729_v5_context_generation_retry_v1';

SELECT encode(
  public.digest(convert_to(result::text, 'UTF8'), 'sha256'),
  'hex'
) AS result_sha256
FROM memory.evidence_extraction_job
WHERE owner_user_id = :'target_owner'::uuid
  AND job_id = :'source_job_id'::uuid
\gset source_

SELECT *
FROM memory.enqueue_owner_context_generation_retry_v1(
  '81000000-0000-4000-8000-000000000001',
  :'source_job_id'::uuid,
  '81000000-0000-4000-8000-000000000002',
  '81000000-0000-4000-8000-000000000003',
  :'content_sha256',
  '20260729_v5_context_generation_retry_v1'
)
\gset applied_

SELECT 1 / ((:'applied_status' = 'pending')::integer);
SELECT 1 / ((:'applied_apply_outcome' = 'applied')::integer);

SELECT *
FROM memory.enqueue_owner_context_generation_retry_v1(
  '81000000-0000-4000-8000-000000000001',
  :'source_job_id'::uuid,
  '81000000-0000-4000-8000-000000000002',
  '81000000-0000-4000-8000-000000000003',
  :'content_sha256',
  '20260729_v5_context_generation_retry_v1'
)
\gset replay_

SELECT 1 / ((:'replay_apply_outcome' = 'replayed')::integer);

SELECT 1 / ((count(*) = 1)::integer)
FROM memory.evidence_extraction_job
WHERE owner_user_id = :'target_owner'::uuid
  AND job_id = '81000000-0000-4000-8000-000000000002'::uuid
  AND evidence_id = :'target_evidence'::uuid
  AND status = 'pending'
  AND attempts = 0
  AND selector_version =
        '20260729_v5_context_generation_retry_v1';

SELECT 1 / ((count(*) = 1)::integer)
FROM memory.evidence_intake_terminal
WHERE owner_user_id = :'target_owner'::uuid
  AND terminal_id = '81000000-0000-4000-8000-000000000003'::uuid
  AND outcome = 'dispatched'
  AND reason_code = 'eligible_dispatched'
  AND details->>'source_job_id' = :'source_job_id';

SELECT 1 / ((count(*) = 1)::integer)
FROM memory.evidence_extraction_event
WHERE owner_user_id = :'target_owner'::uuid
  AND operation_id = '81000000-0000-4000-8000-000000000001'::uuid
  AND actor_ref = 'context_generation_retry_v1'
  AND details->>'source_prose_copied' = 'false';

SELECT 1 / ((count(*) = 1)::integer)
FROM memory.evidence_extraction_job
WHERE owner_user_id = :'target_owner'::uuid
  AND job_id = :'source_job_id'::uuid
  AND status = 'skipped'
  AND attempts = 1
  AND encode(
        public.digest(convert_to(result::text, 'UTF8'), 'sha256'),
        'hex'
      ) = :'source_result_sha256';

SELECT set_config('app.user_id', :'other_owner', true);
SELECT 1 / ((count(*) = 0)::integer)
FROM memory.plan_owner_context_generation_retry_v1(
  :'source_job_id'::uuid
);

DO $cross_owner$
BEGIN
  PERFORM *
  FROM memory.enqueue_owner_context_generation_retry_v1(
    '82000000-0000-4000-8000-000000000001',
    current_setting('test.source_job_id')::uuid,
    '82000000-0000-4000-8000-000000000002',
    '82000000-0000-4000-8000-000000000003',
    current_setting('test.content_sha256'),
    '20260729_v5_context_generation_retry_v1'
  );
  RAISE EXCEPTION 'cross-owner context retry unexpectedly succeeded';
EXCEPTION
  WHEN check_violation THEN NULL;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_context_generation_retry_v1: PASS' AS result;
