\set ON_ERROR_STOP on

BEGIN;

SELECT set_config('test.target_owner', :'target_owner', true);
SELECT set_config('test.other_owner', :'other_owner', true);
SELECT set_config('test.packet_id', :'packet_id', true);
SELECT set_config('test.packet_storage_sha256', :'packet_storage_sha256', true);
SELECT set_config('app.user_id', :'target_owner', true);

DO $test$
DECLARE
  denied boolean:=false;
  applied record;
  replayed record;
BEGIN
  SELECT * INTO applied
  FROM memory.finalize_owner_v5_2_review_deferral_v1(
    '48fb3d93-6a68-5acc-984c-79adfe0fc31f'::uuid,
    '9148a992-b510-51e0-90cf-d9a30cbd161a'::uuid,
    current_setting('test.packet_id')::uuid,
    current_setting('test.packet_storage_sha256'),
    'ambiguous_transcription',
    repeat('a',64)
  );
  SELECT * INTO replayed
  FROM memory.finalize_owner_v5_2_review_deferral_v1(
    '48fb3d93-6a68-5acc-984c-79adfe0fc31f'::uuid,
    '9148a992-b510-51e0-90cf-d9a30cbd161a'::uuid,
    current_setting('test.packet_id')::uuid,
    current_setting('test.packet_storage_sha256'),
    'ambiguous_transcription',
    repeat('a',64)
  );
  IF applied.disposition<>'terminal_no_stage'
     OR applied.review_decision<>'deferred'
     OR applied.reason_code<>'ambiguous_transcription'
     OR applied.promotion_eligible
     OR applied.apply_outcome<>'applied'
     OR replayed.apply_outcome<>'replayed' THEN
    RAISE EXCEPTION 'target rollback-only contract failed';
  END IF;

  PERFORM set_config(
    'app.user_id',current_setting('test.other_owner'),true
  );
  BEGIN
    PERFORM *
    FROM memory.finalize_owner_v5_2_review_deferral_v1(
      '48fb3d93-6a68-5acc-984c-79adfe0fc31f'::uuid,
      '9148a992-b510-51e0-90cf-d9a30cbd161a'::uuid,
      current_setting('test.packet_id')::uuid,
      current_setting('test.packet_storage_sha256'),
      'ambiguous_transcription',
      repeat('a',64)
    );
  EXCEPTION WHEN check_violation THEN
    denied:=true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'cross-owner finalization was not rejected';
  END IF;
END
$test$;

ROLLBACK;

SELECT 'memory_v1_v5_2_ambiguous_review_deferral: PASS' AS result;
