\set ON_ERROR_STOP on

BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';

SELECT set_config('test.target_owner',:'target_owner',false);
SELECT set_config('test.other_owner',:'other_owner',false);
SELECT set_config('test.stance_packet',:'stance_packet',false);
SELECT set_config('test.preference_packet',:'preference_packet',false);
SELECT set_config('test.superseded_packet',:'superseded_packet',false);

DO $catalog$
DECLARE
  relation_name text;
  function_name text;
BEGIN
  IF to_regrole('memory_v5_2_atom_admission_maintainer') IS NULL THEN
    RAISE EXCEPTION 'atom-admission maintainer role is absent';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_2_atom_admission_maintainer'
      AND (rolcanlogin OR rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'atom-admission maintainer role is over-privileged';
  END IF;

  FOREACH relation_name IN ARRAY ARRAY[
    'v5_2_atom_admission_proposal',
    'v5_2_atom_admission_review',
    'v5_2_atom_admission_apply',
    'v5_2_atom_admission_operation'
  ] LOOP
    IF to_regclass('memory.'||relation_name) IS NULL THEN
      RAISE EXCEPTION 'atom-admission relation % is absent',relation_name;
    END IF;
    IF NOT (
      SELECT relrowsecurity AND relforcerowsecurity
      FROM pg_class
      WHERE oid=to_regclass('memory.'||relation_name)
    ) THEN
      RAISE EXCEPTION 'atom-admission relation % is not forced-RLS',relation_name;
    END IF;
    IF pg_get_userbyid((
      SELECT relowner FROM pg_class
      WHERE oid=to_regclass('memory.'||relation_name)
    ))<>'memory_v5_2_atom_admission_maintainer' THEN
      RAISE EXCEPTION 'atom-admission relation % owner is incorrect',
        relation_name;
    END IF;
    IF has_table_privilege('brains_app','memory.'||relation_name,'SELECT')
       OR has_table_privilege('brains_app','memory.'||relation_name,'INSERT')
       OR has_table_privilege('brains_app','memory.'||relation_name,'UPDATE')
       OR has_table_privilege('brains_app','memory.'||relation_name,'DELETE') THEN
      RAISE EXCEPTION 'brains_app has direct atom-admission table access';
    END IF;
  END LOOP;

  FOREACH function_name IN ARRAY ARRAY[
    'memory.plan_owner_v5_2_atom_admission_v1(uuid)',
    'memory.record_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,text,text)',
    'memory.preflight_owner_v5_2_atom_review_v1(uuid,memory.v5_2_atom_review_decision,text,text,jsonb)',
    'memory.review_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text)',
    'memory.preflight_owner_v5_2_atom_apply_v1(uuid)',
    'memory.apply_owner_v5_2_atom_review_v1(uuid,uuid,uuid,text)'
  ] LOOP
    IF NOT has_function_privilege('brains_app',function_name,'EXECUTE') THEN
      RAISE EXCEPTION 'brains_app lacks controlled function %',function_name;
    END IF;
  END LOOP;

  IF has_function_privilege(
       'brains_app',
       'memory.v5_2_atom_stage_projection_authorized_v1(uuid,uuid,jsonb,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app can bypass the stage projection guard';
  END IF;
  IF has_function_privilege(
       'brains_app',
       'memory.guard_v5_2_terminal_evidence_from_stage_v1()',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app can directly execute the stage trigger';
  END IF;
END
$catalog$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id',current_setting('test.target_owner'),false
);

DO $plans$
DECLARE
  stance jsonb;
  preference jsonb;
BEGIN
  stance:=memory.plan_owner_v5_2_atom_admission_v1(
    current_setting('test.stance_packet')::uuid
  );
  IF stance#>>'{counts,source_atom_count}'<>'6'
     OR stance#>>'{counts,admitted_entity_mention_count}'<>'1'
     OR stance#>>'{counts,admitted_observation_count}'<>'4'
     OR stance#>>'{counts,admitted_comparison_hint_count}'<>'0'
     OR stance#>>'{counts,deferred_atom_count}'<>'1'
     OR stance#>>'{counts,retained_source_only_count}'<>'0'
     OR stance#>>'{counts,rejected_atom_count}'<>'0'
     OR jsonb_array_length(stance#>'{stage_projection,entity_mentions}')<>1
     OR jsonb_array_length(stance#>'{stage_projection,observations}')<>4
     OR jsonb_array_length(stance#>'{stage_projection,deferrals}')<>0
     OR stance->>'proposal_sha256' !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'stance atom plan is not the expected 1+4 projection: %',
      stance->'counts';
  END IF;

  preference:=memory.plan_owner_v5_2_atom_admission_v1(
    current_setting('test.preference_packet')::uuid
  );
  IF preference#>>'{counts,source_atom_count}'<>'3'
     OR preference#>>'{counts,admitted_entity_mention_count}'<>'0'
     OR preference#>>'{counts,admitted_observation_count}'<>'0'
     OR preference#>>'{counts,deferred_atom_count}'<>'2'
     OR preference#>>'{counts,retained_source_only_count}'<>'1'
     OR jsonb_array_length(
          preference#>'{stage_projection,observations}'
        )<>0 THEN
    RAISE EXCEPTION 'fully overlapping preference atoms were not held: %',
      preference->'counts';
  END IF;

  BEGIN
    PERFORM memory.plan_owner_v5_2_atom_admission_v1(
      current_setting('test.superseded_packet')::uuid
    );
    RAISE EXCEPTION 'superseded packet produced an atom plan';
  EXCEPTION WHEN check_violation THEN
    IF SQLERRM NOT LIKE '%not the authoritative owner leaf%' THEN
      RAISE;
    END IF;
  END;
END
$plans$;

DO $lifecycle$
DECLARE
  plan_value jsonb;
  proposal_result record;
  review_preflight record;
  review_result record;
  apply_preflight record;
  apply_result record;
BEGIN
  plan_value:=memory.plan_owner_v5_2_atom_admission_v1(
    current_setting('test.stance_packet')::uuid
  );

  SELECT * INTO proposal_result
  FROM memory.record_owner_v5_2_atom_proposal_v1(
    '90000000-0000-4000-8000-000000000001'::uuid,
    '90000000-0000-4000-8000-000000000002'::uuid,
    current_setting('test.stance_packet')::uuid,
    plan_value->>'source_packet_storage_sha256',
    plan_value->>'proposal_sha256'
  );
  IF proposal_result.outcome<>'applied'
     OR proposal_result.admitted_observations<>4
     OR proposal_result.deferred_atoms<>1 THEN
    RAISE EXCEPTION 'proposal persistence outcome is incorrect';
  END IF;
  SELECT * INTO proposal_result
  FROM memory.record_owner_v5_2_atom_proposal_v1(
    '90000000-0000-4000-8000-000000000001'::uuid,
    '90000000-0000-4000-8000-000000000002'::uuid,
    current_setting('test.stance_packet')::uuid,
    plan_value->>'source_packet_storage_sha256',
    plan_value->>'proposal_sha256'
  );
  IF proposal_result.outcome<>'replayed'
     OR proposal_result.admitted_observations<>4
     OR proposal_result.deferred_atoms<>1 THEN
    RAISE EXCEPTION 'proposal replay is not stable';
  END IF;

  SELECT * INTO review_preflight
  FROM memory.preflight_owner_v5_2_atom_review_v1(
    '90000000-0000-4000-8000-000000000002'::uuid,
    'authorized','system',
    'memory_v1_v5_2_atom_admission_policy_v1',
    '["deterministic_atom_projection_reviewed"]'::jsonb
  );
  SELECT * INTO review_result
  FROM memory.review_owner_v5_2_atom_proposal_v1(
    '90000000-0000-4000-8000-000000000003'::uuid,
    '90000000-0000-4000-8000-000000000004'::uuid,
    '90000000-0000-4000-8000-000000000002'::uuid,
    'authorized','system',
    'memory_v1_v5_2_atom_admission_policy_v1',
    '["deterministic_atom_projection_reviewed"]'::jsonb,
    review_preflight.authorization_manifest_sha256
  );
  IF review_result.outcome<>'applied'
     OR review_result.review_number<>1
     OR review_result.decision<>'authorized' THEN
    RAISE EXCEPTION 'atom review persistence outcome is incorrect';
  END IF;
  SELECT * INTO review_result
  FROM memory.review_owner_v5_2_atom_proposal_v1(
    '90000000-0000-4000-8000-000000000003'::uuid,
    '90000000-0000-4000-8000-000000000004'::uuid,
    '90000000-0000-4000-8000-000000000002'::uuid,
    'authorized','system',
    'memory_v1_v5_2_atom_admission_policy_v1',
    '["deterministic_atom_projection_reviewed"]'::jsonb,
    review_preflight.authorization_manifest_sha256
  );
  IF review_result.outcome<>'replayed'
     OR review_result.review_number<>1 THEN
    RAISE EXCEPTION 'atom review replay is not stable';
  END IF;

  SELECT * INTO apply_preflight
  FROM memory.preflight_owner_v5_2_atom_apply_v1(
    '90000000-0000-4000-8000-000000000004'::uuid
  );
  SELECT * INTO apply_result
  FROM memory.apply_owner_v5_2_atom_review_v1(
    '90000000-0000-4000-8000-000000000005'::uuid,
    '90000000-0000-4000-8000-000000000006'::uuid,
    '90000000-0000-4000-8000-000000000004'::uuid,
    apply_preflight.apply_manifest_sha256
  );
  IF apply_result.outcome<>'applied'
     OR apply_result.admitted_observations<>4
     OR apply_result.stage_projection_sha256
          <>plan_value->>'stage_projection_sha256' THEN
    RAISE EXCEPTION 'atom apply persistence outcome is incorrect';
  END IF;
  SELECT * INTO apply_result
  FROM memory.apply_owner_v5_2_atom_review_v1(
    '90000000-0000-4000-8000-000000000005'::uuid,
    '90000000-0000-4000-8000-000000000006'::uuid,
    '90000000-0000-4000-8000-000000000004'::uuid,
    apply_preflight.apply_manifest_sha256
  );
  IF apply_result.outcome<>'replayed'
     OR apply_result.admitted_observations<>4 THEN
    RAISE EXCEPTION 'atom apply replay is not stable';
  END IF;

END
$lifecycle$;

RESET SESSION AUTHORIZATION;
SELECT set_config(
  'app.user_id',current_setting('test.target_owner'),false
);

DO $authorized_stage_guard$
DECLARE
  plan_value jsonb;
BEGIN
  SELECT proposal INTO STRICT plan_value
  FROM memory.v5_2_atom_admission_proposal
  WHERE owner_user_id=current_setting('test.target_owner')::uuid
    AND proposal_id='90000000-0000-4000-8000-000000000002'::uuid;
  INSERT INTO memory.relational_stage_batch(
    owner_user_id,batch_id,evidence_id,
    extraction_packet_text,resolution_packet_text,
    extraction_packet_sha256,resolution_packet_sha256,
    stage_manifest_sha256,extractor,extractor_version,
    mention_count,resolution_count,candidate_count,observation_count,
    temporal_count,result,invoked_by_session
  ) VALUES (
    current_setting('test.target_owner')::uuid,
    '90000000-0000-4000-8000-000000000007'::uuid,
    (plan_value->>'source_evidence_id')::uuid,
    memory.v5_canonical_json_text(plan_value->'stage_projection'),'{}',
    plan_value->>'stage_projection_sha256',memory.v5_digest_text('{}'),
    repeat('2',64),
    'atom_admission_clone_test','v1',1,1,0,4,4,'{}'::jsonb,session_user
  );
END
$authorized_stage_guard$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id',current_setting('test.target_owner'),false
);

DO $later_review$
DECLARE
  deferred_preflight record;
  deferred_result record;
BEGIN
  SELECT * INTO deferred_preflight
  FROM memory.preflight_owner_v5_2_atom_review_v1(
    '90000000-0000-4000-8000-000000000002'::uuid,
    'deferred','system',
    'memory_v1_v5_2_atom_admission_policy_v1',
    '["later_review_invalidates_authorization"]'::jsonb
  );
  SELECT * INTO deferred_result
  FROM memory.review_owner_v5_2_atom_proposal_v1(
    '90000000-0000-4000-8000-000000000008'::uuid,
    '90000000-0000-4000-8000-000000000009'::uuid,
    '90000000-0000-4000-8000-000000000002'::uuid,
    'deferred','system',
    'memory_v1_v5_2_atom_admission_policy_v1',
    '["later_review_invalidates_authorization"]'::jsonb,
    deferred_preflight.authorization_manifest_sha256
  );
  IF deferred_result.review_number<>2
     OR deferred_result.decision<>'deferred' THEN
    RAISE EXCEPTION 'later review was not appended';
  END IF;
END
$later_review$;

RESET SESSION AUTHORIZATION;
SELECT set_config(
  'app.user_id',current_setting('test.target_owner'),false
);

DO $invalidated_stage_guard$
DECLARE
  plan_value jsonb;
BEGIN
  SELECT proposal INTO STRICT plan_value
  FROM memory.v5_2_atom_admission_proposal
  WHERE owner_user_id=current_setting('test.target_owner')::uuid
    AND proposal_id='90000000-0000-4000-8000-000000000002'::uuid;
  BEGIN
    INSERT INTO memory.relational_stage_batch(
      owner_user_id,batch_id,evidence_id,
      extraction_packet_text,resolution_packet_text,
      extraction_packet_sha256,resolution_packet_sha256,
      stage_manifest_sha256,extractor,extractor_version,
      mention_count,resolution_count,candidate_count,observation_count,
      temporal_count,result,invoked_by_session
    ) VALUES (
      current_setting('test.target_owner')::uuid,
      '90000000-0000-4000-8000-000000000010'::uuid,
      (plan_value->>'source_evidence_id')::uuid,
      memory.v5_canonical_json_text(plan_value->'stage_projection'),'{}',
      plan_value->>'stage_projection_sha256',memory.v5_digest_text('{}'),
      repeat('4',64),
      'atom_admission_clone_test','v1',1,1,0,4,4,'{}'::jsonb,session_user
    );
    RAISE EXCEPTION 'invalidated atom projection entered staging';
  EXCEPTION WHEN check_violation THEN
    IF SQLERRM NOT LIKE '%authorized atom projection%' THEN
      RAISE;
    END IF;
  END;
END
$invalidated_stage_guard$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id',current_setting('test.other_owner'),false
);

DO $cross_owner$
BEGIN
  BEGIN
    PERFORM memory.plan_owner_v5_2_atom_admission_v1(
      current_setting('test.stance_packet')::uuid
    );
    RAISE EXCEPTION 'cross-owner actor read target packet';
  EXCEPTION WHEN no_data_found THEN
    IF SQLERRM NOT LIKE '%owner-scoped V5.2 packet is unavailable%' THEN
      RAISE;
    END IF;
  END;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;

DO $rows_and_append_only$
DECLARE
  before_stage integer;
BEGIN
  SELECT count(*) INTO before_stage
  FROM memory.relational_stage_batch
  WHERE owner_user_id=current_setting('test.target_owner')::uuid
    AND batch_id='90000000-0000-4000-8000-000000000007'::uuid;
  IF before_stage<>1
     OR (SELECT count(*) FROM memory.v5_2_atom_admission_proposal)<>1
     OR (SELECT count(*) FROM memory.v5_2_atom_admission_review)<>2
     OR (SELECT count(*) FROM memory.v5_2_atom_admission_apply)<>1
     OR (SELECT count(*) FROM memory.v5_2_atom_admission_operation)<>4 THEN
    RAISE EXCEPTION 'bounded lifecycle row counts are incorrect';
  END IF;

  BEGIN
    UPDATE memory.v5_2_atom_admission_proposal
    SET created_at=created_at
    WHERE proposal_id='90000000-0000-4000-8000-000000000002'::uuid;
    RAISE EXCEPTION 'append-only proposal accepted update';
  EXCEPTION WHEN insufficient_privilege THEN
    IF SQLERRM NOT LIKE '%append-only%' THEN
      RAISE;
    END IF;
  END;
  BEGIN
    DELETE FROM memory.v5_2_atom_admission_review
    WHERE review_id='90000000-0000-4000-8000-000000000004'::uuid;
    RAISE EXCEPTION 'append-only review accepted delete';
  EXCEPTION WHEN insufficient_privilege THEN
    IF SQLERRM NOT LIKE '%append-only%' THEN
      RAISE;
    END IF;
  END;
END
$rows_and_append_only$;

ROLLBACK;

DO $zero_write$
BEGIN
  IF (SELECT count(*) FROM memory.v5_2_atom_admission_proposal)<>0
     OR (SELECT count(*) FROM memory.v5_2_atom_admission_review)<>0
     OR (SELECT count(*) FROM memory.v5_2_atom_admission_apply)<>0
     OR (SELECT count(*) FROM memory.v5_2_atom_admission_operation)<>0
     OR EXISTS (
       SELECT 1 FROM memory.relational_stage_batch
       WHERE batch_id IN (
         '90000000-0000-4000-8000-000000000007'::uuid,
         '90000000-0000-4000-8000-000000000010'::uuid
       )
     ) THEN
    RAISE EXCEPTION 'rollback-only atom admission suite wrote rows';
  END IF;
END
$zero_write$;

SELECT 'memory_v1_v5_2_atom_admission: PASS' AS result;
