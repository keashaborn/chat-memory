BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';

SELECT set_config('test.target_owner',:'target_owner',false);
SELECT set_config('test.other_owner',:'other_owner',false);
SELECT set_config('test.authority_packet',:'authority_packet',false);
SELECT set_config('test.superseded_packet',:'superseded_packet',false);

DO $acl$
BEGIN
  IF has_function_privilege(
       'brains_app',
       'memory.guard_v5_2_terminal_evidence_from_stage_v1()',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app can directly execute the stage trigger';
  END IF;
  IF pg_get_userbyid((
       SELECT proowner FROM pg_proc
       WHERE oid='memory.guard_v5_2_terminal_evidence_from_stage_v1()'
                 ::regprocedure
     ))<>'memory_v5_2_local_router_maintainer' THEN
    RAISE EXCEPTION 'stage trigger owner is incorrect';
  END IF;
END
$acl$;

SELECT set_config(
  'app.user_id',current_setting('test.target_owner'),false
);

DO $exact_deferral_packet_rejected$
BEGIN
  BEGIN
    INSERT INTO memory.relational_stage_batch(
      owner_user_id,batch_id,evidence_id,
      extraction_packet_text,resolution_packet_text,
      extraction_packet_sha256,resolution_packet_sha256,
      stage_manifest_sha256,extractor,extractor_version,
      mention_count,resolution_count,candidate_count,observation_count,
      temporal_count,result
    )
    SELECT
      packet.owner_user_id,gen_random_uuid(),packet.evidence_id,
      packet.normalized_packet::text,'{}',
      packet.validator_packet_sha256,repeat('a',64),repeat('b',64),
      'exact_packet_guard_test','v1',0,0,0,0,0,'{}'::jsonb
    FROM memory.evidence_extraction_packet_v5_local AS packet
    WHERE packet.owner_user_id=current_setting('test.target_owner')::uuid
      AND packet.packet_id=current_setting('test.authority_packet')::uuid;
    RAISE EXCEPTION 'authoritative packet with deferrals entered staging';
  EXCEPTION
    WHEN check_violation THEN
      IF SQLERRM NOT LIKE '%controlled atom-level admission%' THEN
        RAISE;
      END IF;
  END;
END
$exact_deferral_packet_rejected$;

DO $modified_packet_rejected$
BEGIN
  BEGIN
    INSERT INTO memory.relational_stage_batch(
      owner_user_id,batch_id,evidence_id,
      extraction_packet_text,resolution_packet_text,
      extraction_packet_sha256,resolution_packet_sha256,
      stage_manifest_sha256,extractor,extractor_version,
      mention_count,resolution_count,candidate_count,observation_count,
      temporal_count,result
    )
    SELECT
      packet.owner_user_id,gen_random_uuid(),packet.evidence_id,
      jsonb_set(
        packet.normalized_packet,
        '{observations,0,polarity}','"negated"'::jsonb,false
      )::text,
      '{}',packet.validator_packet_sha256,repeat('c',64),repeat('d',64),
      'exact_packet_guard_test','v1',0,0,0,0,0,'{}'::jsonb
    FROM memory.evidence_extraction_packet_v5_local AS packet
    WHERE packet.owner_user_id=current_setting('test.target_owner')::uuid
      AND packet.packet_id=current_setting('test.authority_packet')::uuid;
    RAISE EXCEPTION 'modified V5.2 extraction entered staging';
  EXCEPTION
    WHEN check_violation THEN
      IF SQLERRM NOT LIKE '%differs from immutable extraction%' THEN
        RAISE;
      END IF;
  END;
END
$modified_packet_rejected$;

DO $validator_hash_mismatch_rejected$
BEGIN
  BEGIN
    INSERT INTO memory.relational_stage_batch(
      owner_user_id,batch_id,evidence_id,
      extraction_packet_text,resolution_packet_text,
      extraction_packet_sha256,resolution_packet_sha256,
      stage_manifest_sha256,extractor,extractor_version,
      mention_count,resolution_count,candidate_count,observation_count,
      temporal_count,result
    )
    SELECT
      packet.owner_user_id,gen_random_uuid(),packet.evidence_id,
      packet.normalized_packet::text,'{}',
      repeat('e',64),repeat('f',64),repeat('1',64),
      'exact_packet_guard_test','v1',0,0,0,0,0,'{}'::jsonb
    FROM memory.evidence_extraction_packet_v5_local AS packet
    WHERE packet.owner_user_id=current_setting('test.target_owner')::uuid
      AND packet.packet_id=current_setting('test.authority_packet')::uuid;
    RAISE EXCEPTION 'V5.2 extraction with wrong validator hash entered staging';
  EXCEPTION
    WHEN check_violation THEN
      IF SQLERRM NOT LIKE '%differs from immutable extraction%' THEN
        RAISE;
      END IF;
  END;
END
$validator_hash_mismatch_rejected$;

DO $superseded_packet_rejected$
BEGIN
  BEGIN
    INSERT INTO memory.relational_stage_batch(
      owner_user_id,batch_id,evidence_id,
      extraction_packet_text,resolution_packet_text,
      extraction_packet_sha256,resolution_packet_sha256,
      stage_manifest_sha256,extractor,extractor_version,
      mention_count,resolution_count,candidate_count,observation_count,
      temporal_count,result
    )
    SELECT
      packet.owner_user_id,gen_random_uuid(),packet.evidence_id,
      packet.normalized_packet::text,'{}',
      packet.validator_packet_sha256,repeat('2',64),repeat('3',64),
      'exact_packet_guard_test','v1',0,0,0,0,0,'{}'::jsonb
    FROM memory.evidence_extraction_packet_v5_local AS packet
    WHERE packet.owner_user_id=current_setting('test.target_owner')::uuid
      AND packet.packet_id=current_setting('test.superseded_packet')::uuid;
    RAISE EXCEPTION 'superseded V5.2 extraction entered staging';
  EXCEPTION
    WHEN check_violation THEN
      IF SQLERRM NOT LIKE '%not the authority leaf%' THEN
        RAISE;
      END IF;
  END;
END
$superseded_packet_rejected$;

SELECT set_config(
  'app.user_id',current_setting('test.other_owner'),false
);

DO $cross_owner_rejected$
BEGIN
  BEGIN
    INSERT INTO memory.relational_stage_batch(
      owner_user_id,batch_id,evidence_id,
      extraction_packet_text,resolution_packet_text,
      extraction_packet_sha256,resolution_packet_sha256,
      stage_manifest_sha256,extractor,extractor_version,
      mention_count,resolution_count,candidate_count,observation_count,
      temporal_count,result
    )
    SELECT
      packet.owner_user_id,gen_random_uuid(),packet.evidence_id,
      packet.normalized_packet::text,'{}',
      packet.validator_packet_sha256,repeat('4',64),repeat('5',64),
      'exact_packet_guard_test','v1',0,0,0,0,0,'{}'::jsonb
    FROM memory.evidence_extraction_packet_v5_local AS packet
    WHERE packet.owner_user_id=current_setting('test.target_owner')::uuid
      AND packet.packet_id=current_setting('test.authority_packet')::uuid;
    RAISE EXCEPTION 'cross-owner V5.2 extraction entered staging';
  EXCEPTION
    WHEN insufficient_privilege THEN NULL;
  END;
END
$cross_owner_rejected$;

ROLLBACK;
SELECT 'memory_v1_v5_2_exact_packet_stage_guard: PASS' AS result;
