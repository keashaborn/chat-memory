\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $security$
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5.2 atom stage planner V2 test requires brains_app';
  END IF;
  IF to_regprocedure('memory.plan_owner_v5_2_atom_stage_v2(uuid)') IS NULL THEN
    RAISE EXCEPTION 'V5.2 atom stage planner V2 is absent';
  END IF;
  IF to_regprocedure(
       'memory.plan_owner_v5_2_entity_resolution_review_v1(uuid)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.2 entity review planner is absent';
  END IF;
  IF pg_get_userbyid(
       (SELECT proowner FROM pg_proc
        WHERE oid='memory.plan_owner_v5_2_atom_stage_v2(uuid)'::regprocedure)
     )<>'memory_v5_2_atom_admission_maintainer' THEN
    RAISE EXCEPTION 'V5.2 atom stage planner V2 owner is invalid';
  END IF;
  IF NOT has_function_privilege(
       'brains_app',
       'memory.plan_owner_v5_2_atom_stage_v2(uuid)',
       'EXECUTE'
     )
     OR has_function_privilege(
       'memory_v5_writer',
       'memory.plan_owner_v5_2_atom_stage_v2(uuid)',
       'EXECUTE'
     )
     OR has_function_privilege(
       'memory_v5_2_local_router_maintainer',
       'memory.plan_owner_v5_2_atom_stage_v2(uuid)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.plan_owner_v5_2_entity_resolution_review_v1(uuid)',
       'EXECUTE'
     )
     OR has_function_privilege(
       'memory_v5_2_local_router_maintainer',
       'memory.plan_owner_v5_2_entity_resolution_review_v1(uuid)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'V5.2 atom stage planner V2 ACL is invalid';
  END IF;
END
$security$;

SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

DO $target$
DECLARE
  profession jsonb;
  caregiving jsonb;
BEGIN
  profession:=memory.plan_owner_v5_2_atom_stage_v2(
    'f87ae2b4-57ee-5969-8ce1-bc80a6bfec83'
  );
  caregiving:=memory.plan_owner_v5_2_atom_stage_v2(
    '190f0b21-6e54-5c1d-8a99-6b08358d4846'
  );
  IF profession->>'contract_version'<>'memory_v1_v5_2_atom_stage_plan_v2'
     OR profession->>'policy_version'<>'memory_v1_v5_2_atom_stage_policy_v2'
     OR profession->>'owner_user_id'<>'1240822d-ac9a-4096-95aa-e2b24d36ef50'
     OR profession->>'proposal_id'<>'d45902c5-e52d-59b5-9c80-738709911d57'
     OR profession->>'review_id'<>'4832d9d6-318b-5dcf-b623-4e23e2659e89'
     OR profession->>'packet_id'<>'6ae4a8e6-b207-5201-a997-53fb2363fc9d'
     OR profession->>'evidence_id'<>'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5'
     OR profession->'counts'<>jsonb_build_object(
       'entity_mentions',3,'observations',2,'comparison_hints',0,'deferrals',0
     )
     OR caregiving->>'contract_version'<>'memory_v1_v5_2_atom_stage_plan_v2'
     OR caregiving->>'policy_version'<>'memory_v1_v5_2_atom_stage_policy_v2'
     OR caregiving->>'owner_user_id'<>'1240822d-ac9a-4096-95aa-e2b24d36ef50'
     OR caregiving->>'proposal_id'<>'9a6f2ebb-4a20-5a11-a2d2-c14b0ac471c5'
     OR caregiving->>'review_id'<>'5ac4b908-35da-5891-8fbc-8cdddd5b672c'
     OR caregiving->>'packet_id'<>'b76915b8-0603-50e8-b263-761da39f5651'
     OR caregiving->>'evidence_id'<>'fea59e7e-30f5-4139-b634-97b291c88e14'
     OR caregiving->'counts'<>jsonb_build_object(
       'entity_mentions',2,'observations',2,'comparison_hints',0,'deferrals',0
     ) THEN
    RAISE EXCEPTION 'V5.2 atom stage planner V2 target result is invalid';
  END IF;
END
$target$;

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $cross_owner$
BEGIN
  BEGIN
    PERFORM memory.plan_owner_v5_2_atom_stage_v2(
      'f87ae2b4-57ee-5969-8ce1-bc80a6bfec83'
    );
    RAISE EXCEPTION 'cross-owner profession stage plan unexpectedly resolved';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN NULL;
  END;
  BEGIN
    PERFORM memory.plan_owner_v5_2_atom_stage_v2(
      '190f0b21-6e54-5c1d-8a99-6b08358d4846'
    );
    RAISE EXCEPTION 'cross-owner caregiving stage plan unexpectedly resolved';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN NULL;
  END;
  BEGIN
    PERFORM memory.plan_owner_v5_2_entity_resolution_review_v1(
      'fea59e7e-30f5-4139-b634-97b291c88e14'
    );
    RAISE EXCEPTION 'cross-owner entity review plan unexpectedly resolved';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN NULL;
  END;
END
$cross_owner$;

ROLLBACK;
