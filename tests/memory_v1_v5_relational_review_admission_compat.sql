\set ON_ERROR_STOP on

DO $contract$
DECLARE
  function_oid regprocedure :=
    'memory.record_owner_v5_local_review_artifact_v1(uuid,uuid,uuid,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)';
  definition text := pg_get_functiondef(
    'memory.record_owner_v5_local_review_artifact_v1(uuid,uuid,uuid,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)'::regprocedure
  );
BEGIN
  IF position('NOT packet.manual_review_required' IN definition)>0
     OR NOT has_function_privilege('brains_app',function_oid,'EXECUTE') THEN
    RAISE EXCEPTION 'accepted relational review admission is unavailable';
  END IF;
END
$contract$;

SELECT packet_storage_sha256,entity_mention_count
FROM memory.evidence_extraction_packet_v5_local
WHERE owner_user_id=:'owner_user_id'::uuid
  AND packet_id=:'packet_id'::uuid
  AND NOT manual_review_required
  AND local_model_calls=1
  AND external_model_calls=0
\gset target_

SELECT gen_random_uuid() AS operation_id,
       gen_random_uuid() AS artifact_id,
       gen_random_uuid() AS review_id,
       gen_random_uuid() AS request_id
\gset generated_

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner_user_id', true);

SELECT 1/((apply_outcome='applied')::integer)
FROM memory.record_owner_v5_local_review_artifact_v1(
  :'generated_operation_id'::uuid,
  :'generated_artifact_id'::uuid,
  :'packet_id'::uuid,
  :'target_packet_storage_sha256',
  :'generated_review_id'::uuid,
  :'generated_request_id'::uuid,
  repeat('a',64),repeat('b',64),repeat('c',40),
  0,:'target_entity_mention_count'::integer,0,0,0
);

SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.record_owner_v5_local_review_artifact_v1(
  :'generated_operation_id'::uuid,
  :'generated_artifact_id'::uuid,
  :'packet_id'::uuid,
  :'target_packet_storage_sha256',
  :'generated_review_id'::uuid,
  :'generated_request_id'::uuid,
  repeat('a',64),repeat('b',64),repeat('c',40),
  0,:'target_entity_mention_count'::integer,0,0,0
);

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_relational_review_admission_compat: PASS' AS result;
