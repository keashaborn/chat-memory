\set ON_ERROR_STOP on

BEGIN;

SELECT set_config('app.user_id', :'target_owner', true);
SELECT set_config('test.evidence_id', :'evidence_id', true);

DO $test$
DECLARE
  denied boolean:=false;
BEGIN
  BEGIN
    INSERT INTO memory.relational_stage_batch(
      owner_user_id,batch_id,evidence_id,
      extraction_packet_text,resolution_packet_text,
      extraction_packet_sha256,resolution_packet_sha256,
      stage_manifest_sha256,extractor,extractor_version,
      mention_count,resolution_count,candidate_count,
      observation_count,temporal_count,result,invoked_by_session
    ) VALUES (
      current_setting('app.user_id')::uuid,
      gen_random_uuid(),
      current_setting('test.evidence_id')::uuid,
      '{}','{}',
      encode(digest(convert_to('{}','UTF8'),'sha256'),'hex'),
      encode(digest(convert_to('{}','UTF8'),'sha256'),'hex'),
      encode(digest(convert_to(gen_random_uuid()::text,'UTF8'),'sha256'),'hex'),
      'disposed_evidence_guard_test','v1',0,0,0,0,0,'{}','sage'
    );
  EXCEPTION WHEN check_violation THEN
    IF SQLERRM='disposed evidence cannot enter relational staging' THEN
      denied:=true;
    ELSE
      RAISE;
    END IF;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'disposed evidence entered relational staging';
  END IF;
END
$test$;

ROLLBACK;

SELECT 'memory_v1_v5_2_ambiguous_review_deferral_stage_guard: PASS' AS result;
