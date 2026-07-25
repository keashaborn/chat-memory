\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $security$
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5.2 entity apply planner test requires brains_app';
  END IF;
  IF to_regprocedure(
       'memory.plan_owner_v5_2_entity_apply_review_v1(uuid)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.2 entity apply planner is absent';
  END IF;
  IF pg_get_userbyid(
       (SELECT proowner FROM pg_proc WHERE oid=
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
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

DO $target$
DECLARE
  caregiving jsonb;
  profession jsonb;
BEGIN
  caregiving:=memory.plan_owner_v5_2_entity_apply_review_v1(
    'fea59e7e-30f5-4139-b634-97b291c88e14'
  );
  profession:=memory.plan_owner_v5_2_entity_apply_review_v1(
    'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5'
  );
  IF caregiving->>'contract_version'
       <>'memory_v1_v5_2_entity_apply_review_plan_v1'
     OR caregiving->>'policy_version'
       <>'memory_v1_v5_2_entity_apply_review_policy_v1'
     OR caregiving->>'owner_user_id'
       <>'1240822d-ac9a-4096-95aa-e2b24d36ef50'
     OR jsonb_array_length(caregiving->'items')<>2
     OR jsonb_array_length(profession->'items')<>3
     OR EXISTS (
       SELECT 1
       FROM jsonb_array_elements(
         (caregiving->'items') || (profession->'items')
       ) AS item(value)
       WHERE (item.value->>'existing_apply_count')::integer<>0
     )
     OR (
       SELECT count(*)
       FROM jsonb_array_elements(
         (caregiving->'items') || (profession->'items')
       ) AS item(value)
       WHERE item.value->>'action'='link_existing'
         AND item.value->>'decision_state'='auto_link_eligible'
         AND item.value->>'entity_type'='self'
         AND item.value->>'latest_review_id' IS NULL
     )<>2
     OR (
       SELECT count(*)
       FROM jsonb_array_elements(
         (caregiving->'items') || (profession->'items')
       ) AS item(value)
       WHERE item.value->>'action'='create_new'
         AND item.value->>'decision_state'='manual_review_required'
         AND item.value->>'latest_review_decision'='approved'
     )<>3 THEN
    RAISE EXCEPTION 'V5.2 entity apply planner target result is invalid';
  END IF;
END
$target$;

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $cross_owner$
BEGIN
  BEGIN
    PERFORM memory.plan_owner_v5_2_entity_apply_review_v1(
      'fea59e7e-30f5-4139-b634-97b291c88e14'
    );
    RAISE EXCEPTION 'cross-owner caregiving apply plan unexpectedly resolved';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN NULL;
  END;
  BEGIN
    PERFORM memory.plan_owner_v5_2_entity_apply_review_v1(
      'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5'
    );
    RAISE EXCEPTION 'cross-owner profession apply plan unexpectedly resolved';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN NULL;
  END;
END
$cross_owner$;

ROLLBACK;
