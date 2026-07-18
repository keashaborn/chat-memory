\set ON_ERROR_STOP on

BEGIN;

SELECT 1/(
  (
    SELECT rolcanlogin=false
       AND rolsuper=false
       AND rolcreatedb=false
       AND rolcreaterole=false
       AND rolinherit=false
       AND rolbypassrls=false
    FROM pg_roles
    WHERE rolname='memory_v5_local_review_reader'
  )::integer
);

SELECT 1/(
  (
    SELECT prosecdef
       AND proowner='memory_v5_local_review_reader'::regrole
       AND proconfig=ARRAY['search_path=pg_catalog']::text[]
    FROM pg_proc
    WHERE oid='memory.read_owner_v5_local_packet_review_v1(uuid)'::regprocedure
  )::integer
);

SELECT 1/(
  (
    has_function_privilege(
      'brains_app',
      'memory.read_owner_v5_local_packet_review_v1(uuid)',
      'EXECUTE'
    )
    AND NOT EXISTS (
      SELECT 1
      FROM pg_proc AS function
      CROSS JOIN LATERAL aclexplode(
        coalesce(function.proacl,acldefault('f',function.proowner))
      ) AS privilege
      WHERE function.oid=
        'memory.read_owner_v5_local_packet_review_v1(uuid)'::regprocedure
        AND privilege.grantee=0 AND privilege.privilege_type='EXECUTE'
    )
    AND NOT has_table_privilege(
      'brains_app','memory.evidence_extraction_packet_v5_local','SELECT'
    )
    AND has_table_privilege(
      'memory_v5_local_review_reader',
      'memory.evidence_extraction_packet_v5_local',
      'SELECT'
    )
    AND has_table_privilege(
      'memory_v5_local_review_reader','memory.evidence','SELECT'
    )
    AND has_table_privilege(
      'memory_v5_local_review_reader',
      'memory.evidence_extraction_job',
      'SELECT'
    )
    AND has_table_privilege(
      'memory_v5_local_review_reader',
      'memory.relational_stage_batch',
      'SELECT'
    )
    AND NOT has_table_privilege(
      'memory_v5_local_review_reader',
      'memory.evidence_extraction_packet_v5_local',
      'INSERT,UPDATE,DELETE'
    )
  )::integer
);

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'target_owner', true);

SELECT 1/(
  (
    SELECT count(*)=1
       AND bool_and(packet_id=:'target_packet'::uuid)
       AND bool_and(provider_id='local_llama_cpp')
       AND bool_and(local_model_calls=1)
       AND bool_and(external_model_calls=0)
       AND bool_and(storage_integrity_verified)
       AND bool_and(job_status='review_required')
       AND bool_and(job_route='relational_extraction')
       AND bool_and(NOT job_lease_present)
       AND bool_and(NOT job_error_present)
       AND bool_and(evidence_status='active')
       AND bool_and(exact_stage_batch_count=0)
       AND bool_and(evidence_stage_batch_count=0)
    FROM memory.read_owner_v5_local_packet_review_v1(
      :'target_packet'::uuid
    )
  )::integer
);

SELECT set_config('app.user_id', :'other_owner', true);
SELECT 1/(
  (
    SELECT count(*)=0
    FROM memory.read_owner_v5_local_packet_review_v1(
      :'target_packet'::uuid
    )
  )::integer
);

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_packet_review_read: PASS' AS result;
