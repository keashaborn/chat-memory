\set ON_ERROR_STOP on

SELECT set_config('test.prior_packet',:'prior_packet',false);
SELECT set_config('test.content_sha256',:'content_sha256',false);
SELECT set_config(
  'test.packet_storage_sha256',:'packet_storage_sha256',false
);

DO $catalog$
BEGIN
  IF to_regrole('memory_v5_local_reextract_maintainer') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_local_semantic_reextract_v3(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.enqueue_owner_v5_local_semantic_reextract_v3(uuid,uuid,uuid,uuid,text,text,text,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'local semantic reextract v3 objects are absent';
  END IF;
  IF (SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
             OR rolinherit OR rolbypassrls
      FROM pg_roles
      WHERE rolname='memory_v5_local_reextract_maintainer')
     OR has_table_privilege(
       'brains_app','memory.evidence_extraction_packet_v5_local','SELECT'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_extraction_job','INSERT'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.plan_owner_v5_local_semantic_reextract_v3(uuid)','EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.enqueue_owner_v5_local_semantic_reextract_v3(uuid,uuid,uuid,uuid,text,text,text,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'local semantic reextract v3 ACL is unsafe';
  END IF;
END
$catalog$;

BEGIN;
SET LOCAL statement_timeout='30s';
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'target_owner',true);

SELECT 1/((count(*)=1)::integer)
FROM memory.plan_owner_v5_local_semantic_reextract_v3(:'prior_packet'::uuid)
WHERE evidence_content_sha256=:'content_sha256'
  AND prior_packet_storage_sha256=:'packet_storage_sha256'
  AND reason_code='semantic_relationship_role_mismatch'
  AND next_selector_version='20260720_v5_semantic_guard_reextract_v1'
  AND next_policy_compiler_version='memory_v1_local_policy_compiler_v7';

SELECT * FROM memory.enqueue_owner_v5_local_semantic_reextract_v3(
  '78000000-0000-4000-8000-000000000001',
  '78000000-0000-4000-8000-000000000002',
  '78000000-0000-4000-8000-000000000003',
  :'prior_packet'::uuid,:'content_sha256',:'packet_storage_sha256',
  '20260720_v5_semantic_guard_reextract_v1',
  'memory_v1_local_policy_compiler_v7'
) \gset applied_
SELECT 1/((:'applied_status'='pending')::integer),
       1/((:'applied_apply_outcome'='applied')::integer);

SELECT * FROM memory.enqueue_owner_v5_local_semantic_reextract_v3(
  '78000000-0000-4000-8000-000000000001',
  '78000000-0000-4000-8000-000000000002',
  '78000000-0000-4000-8000-000000000003',
  :'prior_packet'::uuid,:'content_sha256',:'packet_storage_sha256',
  '20260720_v5_semantic_guard_reextract_v1',
  'memory_v1_local_policy_compiler_v7'
) \gset replay_
SELECT 1/((:'replay_apply_outcome'='replayed')::integer);

SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_job
WHERE owner_user_id=:'target_owner'::uuid
  AND job_id='78000000-0000-4000-8000-000000000002'::uuid
  AND status='pending' AND attempts=0
  AND selector_version='20260720_v5_semantic_guard_reextract_v1';
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_intake_terminal
WHERE owner_user_id=:'target_owner'::uuid
  AND terminal_id='78000000-0000-4000-8000-000000000003'::uuid
  AND outcome='dispatched' AND reason_code='eligible_dispatched';
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_event
WHERE owner_user_id=:'target_owner'::uuid
  AND operation_id='78000000-0000-4000-8000-000000000001'::uuid
  AND actor_ref='local_semantic_reextract_v3'
  AND details->>'source_prose_copied'='false';

SELECT set_config('app.user_id',:'other_owner',true);
SELECT 1/((count(*)=0)::integer)
FROM memory.plan_owner_v5_local_semantic_reextract_v3(:'prior_packet'::uuid);
DO $cross_owner$
BEGIN
  PERFORM * FROM memory.enqueue_owner_v5_local_semantic_reextract_v3(
    '79000000-0000-4000-8000-000000000001',
    '79000000-0000-4000-8000-000000000002',
    '79000000-0000-4000-8000-000000000003',
    current_setting('test.prior_packet')::uuid,
    current_setting('test.content_sha256'),
    current_setting('test.packet_storage_sha256'),
    '20260720_v5_semantic_guard_reextract_v1',
    'memory_v1_local_policy_compiler_v7'
  );
  RAISE EXCEPTION 'cross-owner semantic reextract v3 unexpectedly succeeded';
EXCEPTION WHEN check_violation THEN NULL;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_semantic_reextract_v3: PASS' AS result;
