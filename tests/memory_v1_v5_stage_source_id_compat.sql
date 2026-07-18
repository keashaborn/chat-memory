\set ON_ERROR_STOP on

BEGIN;

SELECT 1 / (
  has_function_privilege(
    'brains_app',
    'memory.preflight_relational_stage_bundle_v5(uuid,text,text,timestamptz)',
    'EXECUTE'
  )
  AND has_function_privilege(
    'brains_app',
    'memory.stage_relational_packet_v5(uuid,uuid,text,text,text,text,text,text)',
    'EXECUTE'
  )
  AND NOT has_table_privilege('brains_app','memory.evidence','SELECT')
)::integer AS assert_controlled_acl;

SELECT set_config('test.evidence_id', :'evidence_id', true);
SELECT set_config('test.source_id', :'source_id', true);
SELECT set_config('test.source_sha256', :'source_sha256', true);
SELECT set_config('test.source_recorded_at', :'source_recorded_at', true);

SET SESSION AUTHORIZATION brains_app;

DO $test$
BEGIN
  BEGIN
    PERFORM *
    FROM memory.preflight_relational_stage_bundle_v5(
      current_setting('test.evidence_id')::uuid,
      current_setting('test.source_id'),
      current_setting('test.source_sha256'),
      current_setting('test.source_recorded_at')::timestamptz
    );
    RAISE EXCEPTION 'missing actor unexpectedly accepted';
  EXCEPTION
    WHEN insufficient_privilege THEN NULL;
  END;
END
$test$;

SELECT set_config('app.user_id', :'target_owner', true);

SELECT 1 / (
  SELECT verified::integer
  FROM memory.preflight_relational_stage_bundle_v5(
    :'evidence_id'::uuid,
    :'source_id',
    :'source_sha256',
    :'source_recorded_at'::timestamptz
  )
)::integer AS assert_owner_alias_accepted;

SELECT set_config('app.user_id', :'other_owner', true);

DO $test$
BEGIN
  BEGIN
    PERFORM *
    FROM memory.preflight_relational_stage_bundle_v5(
      current_setting('test.evidence_id')::uuid,
      current_setting('test.source_id'),
      current_setting('test.source_sha256'),
      current_setting('test.source_recorded_at')::timestamptz
    );
    RAISE EXCEPTION 'cross-owner source alias unexpectedly accepted';
  EXCEPTION
    WHEN no_data_found THEN NULL;
  END;
END
$test$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_stage_source_id_compat: PASS' AS result;
