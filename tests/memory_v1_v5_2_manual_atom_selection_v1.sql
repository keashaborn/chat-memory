BEGIN;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

DO $security$
DECLARE
  owner_id constant uuid :=
    '1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid;
  target_packet_id constant uuid :=
    'c4db1405-ad9d-5c9a-81e3-10ad0200b0ca'::uuid;
  evidence_id constant uuid :=
    '681ab38d-a742-463c-ad26-c74c65eacaa9'::uuid;
  selection_id constant uuid :=
    '6cfb3ef5-7c85-4035-92ae-8b5c8d71ed14'::uuid;
  operation_id constant uuid :=
    'a43cbe80-b1d5-4be4-940f-7d9ebed02960'::uuid;
  packet_sha constant text :=
    '90baad563bd05cdaf93f9d9b72ec5593e86592e4e0f334143a287e5f5e76dc5d';
  baseline_sha constant text :=
    'bfed594b759d942701b51c9275d0d8e7ab6b4c6e529c09af8b6fdb4f19ebd9af';
  review_sha constant text :=
    'f412bb26c8cefea3dd1b100c6a2d6eec7654d637fb4fdda5763263727fd50748';
  entity_refs constant jsonb := '["e00"]'::jsonb;
  observation_refs constant jsonb := '["o02","o03"]'::jsonb;
  manifest text;
  result record;
  plan jsonb;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid='memory.v5_2_manual_atom_selection_v1'::regclass
      AND relrowsecurity AND relforcerowsecurity
  ) OR has_table_privilege(
    'brains_app','memory.v5_2_manual_atom_selection_v1','SELECT'
  ) OR has_table_privilege(
    'brains_app','memory.v5_2_manual_atom_selection_v1','INSERT'
  ) THEN
    RAISE EXCEPTION 'manual atom selection RLS or ACL boundary failed';
  END IF;
  IF NOT has_function_privilege(
    'brains_app',
    'memory.register_owner_v5_2_manual_atom_selection_v1(uuid,uuid,uuid,text,text,text,jsonb,jsonb,text)',
    'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'restricted manual selection entrypoint is absent';
  END IF;
  IF memory.plan_owner_v5_2_atom_admission_v2_before_manual_selection_v1(
       target_packet_id
     )->>'proposal_sha256'<>baseline_sha THEN
    RAISE EXCEPTION 'automatic baseline plan drifted';
  END IF;

  manifest:=memory.v5_2_manual_atom_selection_manifest_sha_v1(
    owner_id,selection_id,target_packet_id,evidence_id,packet_sha,baseline_sha,
    review_sha,entity_refs,observation_refs
  );
  SELECT * INTO STRICT result
  FROM memory.register_owner_v5_2_manual_atom_selection_v1(
    selection_id,operation_id,target_packet_id,packet_sha,baseline_sha,
    review_sha,entity_refs,observation_refs,manifest
  );
  IF result.outcome<>'applied'
     OR result.approved_entity_count<>1
     OR result.approved_observation_count<>2 THEN
    RAISE EXCEPTION 'manual atom selection did not apply exactly';
  END IF;
  SELECT * INTO STRICT result
  FROM memory.register_owner_v5_2_manual_atom_selection_v1(
    selection_id,operation_id,target_packet_id,packet_sha,baseline_sha,
    review_sha,entity_refs,observation_refs,manifest
  );
  IF result.outcome<>'replayed' THEN
    RAISE EXCEPTION 'manual atom selection replay wrote again';
  END IF;

  plan:=memory.plan_owner_v5_2_atom_admission_v2(target_packet_id);
  IF plan#>>'{counts,source_atom_count}'<>'6'
     OR plan#>>'{counts,admitted_entity_mention_count}'<>'1'
     OR plan#>>'{counts,admitted_observation_count}'<>'2'
     OR plan#>>'{counts,deferred_atom_count}'<>'3'
     OR plan#>>'{counts,retained_source_only_count}'<>'0'
     OR jsonb_array_length(plan#>'{stage_projection,entity_mentions}')<>1
     OR jsonb_array_length(plan#>'{stage_projection,observations}')<>2
     OR jsonb_array_length(plan#>'{stage_projection,deferrals}')<>0
     OR (SELECT array_agg(item->>'observation_ref' ORDER BY item->>'observation_ref')
         FROM jsonb_array_elements(plan#>'{stage_projection,observations}') item)
        <>ARRAY['o02','o03']::text[]
     OR plan#>>'{stage_projection,entity_mentions,0,name_text}'<>'Jerry'
     OR plan#>>'{stage_projection,entity_mentions,0,relationship_role}'
        <>'family:father' THEN
    RAISE EXCEPTION 'manual atom stage projection drifted: %',plan;
  END IF;
  BEGIN
    INSERT INTO memory.v5_2_manual_atom_selection_v1(
      selection_id,owner_user_id,operation_id,packet_id,evidence_id,
      packet_storage_sha256,baseline_proposal_sha256,
      manual_review_report_sha256,approved_entity_refs,
      approved_observation_refs,selection_manifest_sha256,
      policy_version,decision
    ) VALUES (
      gen_random_uuid(),owner_id,gen_random_uuid(),target_packet_id,evidence_id,
      packet_sha,baseline_sha,review_sha,entity_refs,observation_refs,
      repeat('a',64),'memory_v1_v5_2_manual_atom_selection_policy_v1',
      'authorized'
    );
    RAISE EXCEPTION 'brains_app directly inserted a manual selection';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END
$security$;

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $cross_owner$
BEGIN
  BEGIN
    PERFORM memory.plan_owner_v5_2_atom_admission_v2(
      'c4db1405-ad9d-5c9a-81e3-10ad0200b0ca'::uuid
    );
    RAISE EXCEPTION 'cross-owner atom plan unexpectedly resolved';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$cross_owner$;

ROLLBACK;
SELECT 'memory_v1_v5_2_manual_atom_selection_v1: PASS' AS result;
