\set ON_ERROR_STOP on

SELECT set_config('test.owner_user_id',:'owner_user_id',false);
SELECT set_config('test.packet_id',:'packet_id',false);
SELECT set_config('test.packet_storage_sha256',:'packet_storage_sha256',false);

DO $catalog$
DECLARE
  function_definition text;
BEGIN
  IF to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regprocedure(
       'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'compatible local packet disposition objects are absent';
  END IF;
  SELECT pg_get_functiondef(
    'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'::regprocedure
  ) INTO function_definition;
  IF strpos(
    function_definition,
    'packet.local_model_calls NOT BETWEEN 0 AND 1'
  )=0 OR strpos(function_definition,'packet.local_model_calls<>1')>0 THEN
    RAISE EXCEPTION 'terminal-deferral function is not zero-call compatible';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM memory.evidence_extraction_packet_v5_local
    WHERE owner_user_id=current_setting('test.owner_user_id')::uuid
      AND packet_id=current_setting('test.packet_id')::uuid
      AND packet_storage_sha256=current_setting('test.packet_storage_sha256')
      AND local_model_calls=0 AND external_model_calls=0
      AND entity_mention_count=0 AND observation_count=0
      AND comparison_hint_count=0 AND deferral_count>0
  ) THEN
    RAISE EXCEPTION 'zero-call terminal-deferral fixture is absent';
  END IF;
  IF has_table_privilege(
       'brains_app','memory.v5_local_packet_disposition','INSERT'
     ) OR NOT has_function_privilege(
       'brains_app',
       'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'compatible disposition ACL is unsafe';
  END IF;
END
$catalog$;

BEGIN;
SET LOCAL statement_timeout='30s';
SELECT set_config('test.packet_id',:'packet_id',true);
SELECT set_config('test.packet_storage_sha256',:'packet_storage_sha256',true);
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_user_id',true);

DO $apply$
DECLARE
  result record;
BEGIN
  IF (SELECT count(*) FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
      WHERE packet_id=current_setting('test.packet_id')::uuid
        AND disposition_route='terminal_deferral')<>1 THEN
    RAISE EXCEPTION 'zero-call terminal-deferral plan is absent';
  END IF;
  SELECT * INTO result FROM memory.finalize_owner_v5_local_deferral_v1(
    '30000000-0000-4000-8000-000000000001',
    '30000000-0000-4000-8000-000000000002',
    current_setting('test.packet_id')::uuid,
    current_setting('test.packet_storage_sha256'),
    'deferral_only_no_stage'
  );
  IF result.apply_outcome<>'applied'
     OR result.disposition<>'terminal_no_stage' THEN
    RAISE EXCEPTION 'zero-call terminal deferral was not applied';
  END IF;
  SELECT * INTO result FROM memory.finalize_owner_v5_local_deferral_v1(
    '30000000-0000-4000-8000-000000000001',
    '30000000-0000-4000-8000-000000000002',
    current_setting('test.packet_id')::uuid,
    current_setting('test.packet_storage_sha256'),
    'deferral_only_no_stage'
  );
  IF result.apply_outcome<>'replayed' THEN
    RAISE EXCEPTION 'zero-call terminal deferral replay wrote again';
  END IF;
END
$apply$;

SELECT set_config('app.user_id',:'other_owner_user_id',true);
DO $isolation$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
    WHERE packet_id=current_setting('test.packet_id')::uuid
  ) THEN
    RAISE EXCEPTION 'zero-call packet crossed owner boundary';
  END IF;
  BEGIN
    PERFORM * FROM memory.finalize_owner_v5_local_deferral_v1(
      '40000000-0000-4000-8000-000000000001',
      '40000000-0000-4000-8000-000000000002',
      current_setting('test.packet_id')::uuid,
      current_setting('test.packet_storage_sha256'),
      'deferral_only_no_stage'
    );
    RAISE EXCEPTION 'cross-owner zero-call disposition unexpectedly succeeded';
  EXCEPTION WHEN check_violation THEN
    NULL;
  END;
END
$isolation$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_packet_disposition_zero_call_compat: PASS' AS result;
