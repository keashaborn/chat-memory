\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $security$
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'reviewed pet entity apply planner test requires brains_app';
  END IF;
  IF to_regprocedure(
       'memory.plan_owner_v5_2_entity_apply_review_v1(uuid)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.2 entity apply planner is absent';
  END IF;
  IF pg_get_userbyid(
       (SELECT proowner
          FROM pg_proc
         WHERE oid=
           'memory.plan_owner_v5_2_entity_apply_review_v1(uuid)'::regprocedure)
     )<>'memory_v5_writer'
     OR NOT has_function_privilege(
       'brains_app',
       'memory.plan_owner_v5_2_entity_apply_review_v1(uuid)',
       'EXECUTE'
     )
     OR has_function_privilege(
       'memory_v5_2_local_router_maintainer',
       'memory.plan_owner_v5_2_entity_apply_review_v1(uuid)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'V5.2 entity apply planner ownership or ACL is invalid';
  END IF;
END
$security$;

SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $target$
DECLARE
  keasha jsonb;
  dahlia jsonb;
  helsing jsonb;
  combined jsonb;
BEGIN
  keasha:=memory.plan_owner_v5_2_entity_apply_review_v1(
    'c5d6f5cf-c6d6-554c-85d9-02150e8b8ae7'
  );
  dahlia:=memory.plan_owner_v5_2_entity_apply_review_v1(
    '2be95051-30c8-5f87-8ff9-e0999600b447'
  );
  helsing:=memory.plan_owner_v5_2_entity_apply_review_v1(
    '88161526-0c53-5291-8601-0bd0ba41da53'
  );
  combined:=(keasha->'items') || (dahlia->'items') || (helsing->'items');

  IF keasha->>'contract_version'
       <>'memory_v1_v5_2_entity_apply_review_plan_v1'
     OR keasha->>'policy_version'
       <>'memory_v1_v5_2_entity_apply_review_policy_v1'
     OR keasha->>'owner_user_id'
       <>'1240822d-ac9a-4096-95aa-e2b24d36ef50'
     OR jsonb_array_length(keasha->'items')<>1
     OR jsonb_array_length(dahlia->'items')<>1
     OR jsonb_array_length(helsing->'items')<>1
     OR EXISTS (
       SELECT 1
         FROM jsonb_array_elements(combined) AS item(value)
        WHERE (item.value->>'existing_apply_count')::integer<>0
           OR (item.value->>'observation_count')::integer<>1
           OR item.value->>'decision_state'<>'manual_review_required'
           OR item.value->>'latest_review_decision'<>'approved'
           OR item.value->>'latest_review_id' IS NULL
     )
     OR (
       SELECT count(*)
         FROM jsonb_array_elements(combined) AS item(value)
        WHERE item.value->>'action'='link_existing'
          AND item.value->>'entity_type'='animal'
          AND item.value->>'selected_entity_id' IS NOT NULL
     )<>2
     OR (
       SELECT count(*)
         FROM jsonb_array_elements(combined) AS item(value)
        WHERE item.value->>'action'='create_new'
          AND item.value->>'entity_type'='animal'
          AND item.value->>'name_text'='Helsing'
          AND item.value->'proposed_entity'->>'canonical_name'='Helsing'
     )<>1 THEN
    RAISE EXCEPTION 'reviewed pet entity apply planner result is invalid';
  END IF;
END
$target$;

SELECT set_config(
  'app.user_id',
  '557ea042-cb82-48f8-9429-472e96c957ef',
  true
);

DO $cross_owner$
DECLARE
  evidence_id uuid;
BEGIN
  FOREACH evidence_id IN ARRAY ARRAY[
    'c5d6f5cf-c6d6-554c-85d9-02150e8b8ae7'::uuid,
    '2be95051-30c8-5f87-8ff9-e0999600b447'::uuid,
    '88161526-0c53-5291-8601-0bd0ba41da53'::uuid
  ]
  LOOP
    BEGIN
      PERFORM memory.plan_owner_v5_2_entity_apply_review_v1(evidence_id);
      RAISE EXCEPTION 'cross-owner reviewed pet apply plan unexpectedly resolved';
    EXCEPTION WHEN SQLSTATE 'P0002' THEN NULL;
    END;
  END LOOP;
END
$cross_owner$;

ROLLBACK;
