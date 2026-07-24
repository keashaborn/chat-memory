BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';

SELECT set_config('test.target_owner',:'target_owner',false);
SELECT set_config('test.other_owner',:'other_owner',false);
SELECT set_config('test.evidence_id',:'evidence_id',false);
SELECT set_config('test.prior_packet',:'prior_packet',false);
SELECT set_config('test.middle_packet',:'middle_packet',false);
SELECT set_config('test.authority_packet',:'authority_packet',false);

DO $acl$
BEGIN
  IF has_function_privilege(
       'brains_app',
       'memory.authoritative_owner_v5_2_packet_id_v1(uuid)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app can directly execute packet authority helper';
  END IF;
  IF NOT has_function_privilege(
       'brains_app',
       'memory.plan_owner_v5_2_local_packet_route_v1(integer)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app lost the bounded V5.2 route planner';
  END IF;
  IF NOT has_table_privilege(
       'memory_v5_2_local_router_maintainer',
       'memory.evidence_extraction_event','SELECT'
     ) THEN
    RAISE EXCEPTION 'router maintainer lacks internal lineage access';
  END IF;
END
$acl$;

SELECT set_config(
  'app.user_id',current_setting('test.target_owner'),false
);

DO $authority$
DECLARE
  selected uuid;
BEGIN
  selected:=memory.authoritative_owner_v5_2_packet_id_v1(
    current_setting('test.evidence_id')::uuid
  );
  IF selected IS DISTINCT FROM
       current_setting('test.authority_packet')::uuid THEN
    RAISE EXCEPTION 'explicit re-extraction lineage selected the wrong leaf';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_2_local_packet_route_v1(25)
    WHERE packet_id IN (
      current_setting('test.prior_packet')::uuid,
      current_setting('test.middle_packet')::uuid
    )
  ) THEN
    RAISE EXCEPTION 'a superseded V5.2 packet remains router-visible';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM memory.v5_2_local_packet_route_event
    WHERE owner_user_id=current_setting('test.target_owner')::uuid
      AND packet_id=current_setting('test.authority_packet')::uuid
      AND route='manual_review_artifact_ready'
  ) THEN
    RAISE EXCEPTION 'authority leaf lacks its append-only review route';
  END IF;
END
$authority$;

SELECT set_config(
  'app.user_id',current_setting('test.other_owner'),false
);

DO $cross_owner$
BEGIN
  IF memory.authoritative_owner_v5_2_packet_id_v1(
       current_setting('test.evidence_id')::uuid
     ) IS NOT NULL THEN
    RAISE EXCEPTION 'packet authority leaked across owners';
  END IF;
END
$cross_owner$;

SELECT set_config(
  'app.user_id',current_setting('test.target_owner'),false
);

DO $superseded_stage_rejected$
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
      encode(public.digest(convert_to(
        packet.normalized_packet::text,'UTF8'
      ),'sha256'),'hex'),
      repeat('a',64),repeat('b',64),'authority_security_test','v1',
      0,0,0,0,0,'{}'::jsonb
    FROM memory.evidence_extraction_packet_v5_local AS packet
    WHERE packet.owner_user_id=current_setting('test.target_owner')::uuid
      AND packet.packet_id=current_setting('test.middle_packet')::uuid;
    RAISE EXCEPTION 'superseded V5.2 packet entered relational staging';
  EXCEPTION
    WHEN check_violation THEN NULL;
  END;
END
$superseded_stage_rejected$;

DO $synthetic_sibling_conflict$
DECLARE
  fixture_terminal_id uuid:='00000000-0000-4000-8000-000000000911';
  fixture_job_id uuid:='00000000-0000-4000-8000-000000000912';
  fixture_packet_id uuid:='00000000-0000-4000-8000-000000000913';
  fixture_operation_id uuid:='00000000-0000-4000-8000-000000000914';
  target_evidence_id uuid:=current_setting('test.evidence_id')::uuid;
  owner_id uuid:=current_setting('test.target_owner')::uuid;
  content_sha text;
  packet_value jsonb;
BEGIN
  SELECT content_sha256 INTO content_sha
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=owner_id
    AND evidence.evidence_id=target_evidence_id;

  INSERT INTO memory.evidence_intake_terminal(
    terminal_id,owner_user_id,evidence_id,selector_version,
    outcome,reason_code,evidence_content_sha256,source_job_id,
    source_job_status,source_job_pipeline_version,decision_fingerprint,
    actor_user_id,invoked_by_role,details
  ) VALUES (
    fixture_terminal_id,owner_id,target_evidence_id,
    'test_authority_conflict_v1',
    'dispatched','eligible_dispatched',content_sha,NULL,NULL,NULL,
    repeat('c',64),owner_id,session_user,'{}'::jsonb
  );

  INSERT INTO memory.evidence_extraction_job(
    job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
    evidence_content_sha256,route,intake_reason_code,status,priority,
    attempts,available_at,lease_token,lease_expires_at,worker_id,
    last_error,result,checkpoint_sequence,checkpoint_sha256
  ) VALUES (
    fixture_job_id,owner_id,target_evidence_id,fixture_terminal_id,
    'test_authority_conflict_v1',
    content_sha,'relational_extraction','eligible_unprocessed',
    'review_required',100,1,clock_timestamp(),NULL,NULL,NULL,NULL,
    '{}'::jsonb,0,NULL
  );

  SELECT jsonb_set(
           normalized_packet,'{source_envelope,job_id}',
           to_jsonb(fixture_job_id::text),false
         )
    INTO packet_value
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id=owner_id
    AND memory.evidence_extraction_packet_v5_local.packet_id=
        current_setting('test.authority_packet')::uuid;

  INSERT INTO memory.evidence_extraction_packet_v5_local(
    packet_id,owner_user_id,operation_id,job_id,evidence_id,
    evidence_content_sha256,provider_id,provider_version,
    provider_model_sha256,model_file_sha256,runtime_revision_sha256,
    policy_compiler_sha256,provider_output_sha256,
    validator_packet_sha256,packet_storage_sha256,normalized_packet,
    manual_review_required,local_model_calls,external_model_calls,
    entity_mention_count,observation_count,comparison_hint_count,
    deferral_count
  )
  SELECT
    fixture_packet_id,owner_id,fixture_operation_id,fixture_job_id,
    target_evidence_id,content_sha,
    source.provider_id,source.provider_version,source.provider_model_sha256,
    source.model_file_sha256,source.runtime_revision_sha256,
    source.policy_compiler_sha256,source.provider_output_sha256,
    encode(public.digest(
      convert_to(packet_value::text,'UTF8'),'sha256'
    ),'hex'),
    encode(public.digest(
      convert_to(packet_value::text,'UTF8'),'sha256'
    ),'hex'),
    packet_value,source.manual_review_required,source.local_model_calls,
    source.external_model_calls,source.entity_mention_count,
    source.observation_count,source.comparison_hint_count,
    source.deferral_count
  FROM memory.evidence_extraction_packet_v5_local AS source
  WHERE source.owner_user_id=owner_id
    AND source.packet_id=current_setting('test.authority_packet')::uuid;

  IF memory.authoritative_owner_v5_2_packet_id_v1(target_evidence_id)
       IS NOT NULL THEN
    RAISE EXCEPTION 'unrelated V5.2 sibling leaves did not fail closed';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_2_local_packet_route_v1(25) AS route_plan
    WHERE route_plan.evidence_id=target_evidence_id
  ) THEN
    RAISE EXCEPTION 'ambiguous V5.2 sibling leaves remained router-visible';
  END IF;
END
$synthetic_sibling_conflict$;

ROLLBACK;
SELECT 'memory_v1_v5_2_packet_authority_compat: PASS' AS result;
