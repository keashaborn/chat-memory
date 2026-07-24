BEGIN;

DO $test$
DECLARE
  function_owner text;
BEGIN
  IF to_regclass('memory.v5_2_local_packet_route_event') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_2_local_packet_route_v1(integer)'
     ) IS NULL
     OR to_regprocedure(
       'memory.finalize_owner_v5_2_terminal_route_v1(uuid,uuid,uuid,text,text,text[])'
     ) IS NULL
     OR to_regprocedure(
       'memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.2 local packet router objects are absent';
  END IF;

  SELECT pg_get_userbyid(proowner) INTO function_owner
  FROM pg_proc
  WHERE oid='memory.plan_owner_v5_2_local_packet_route_v1(integer)'::regprocedure;
  IF function_owner<>'memory_v5_2_local_router_maintainer' THEN
    RAISE EXCEPTION 'V5.2 route planner has the wrong owner';
  END IF;

  IF NOT has_function_privilege(
       'brains_app',
       'memory.plan_owner_v5_2_local_packet_route_v1(integer)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.finalize_owner_v5_2_terminal_route_v1(uuid,uuid,uuid,text,text,text[])',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)',
       'EXECUTE'
     )
     OR has_table_privilege(
       'brains_app','memory.v5_2_local_packet_route_event','SELECT'
     )
     OR has_table_privilege(
       'brains_app','memory.v5_2_local_packet_route_event','INSERT'
     )
     OR has_function_privilege(
       'brains_app',
       'memory.guard_v5_2_terminal_evidence_from_stage_v1()',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'V5.2 local packet router ACL is unsafe';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_class
    WHERE oid='memory.v5_2_local_packet_route_event'::regclass
      AND relrowsecurity
      AND relforcerowsecurity
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid='memory.v5_2_local_packet_route_event'::regclass
      AND tgname='v5_2_local_packet_route_event_append_only_guard'
      AND tgenabled='O'
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid='memory.relational_stage_batch'::regclass
      AND tgname='v5_2_terminal_evidence_stage_guard'
      AND tgenabled='O'
  ) THEN
    RAISE EXCEPTION 'V5.2 local packet router guards are absent';
  END IF;

  IF has_function_privilege(
       'memory_v5_2_local_router_maintainer',
       'memory.stage_relational_packet_v5_2(uuid,uuid,text,text,text,text,text,text)',
       'EXECUTE'
     )
     OR has_function_privilege(
       'memory_v5_2_local_router_maintainer',
       'memory.apply_entity_resolution_v5_2(uuid,uuid,uuid,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'V5.2 router maintainer can reach downstream apply';
  END IF;
END
$test$;

SET LOCAL ROLE brains_app;

SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $owner_plan$
DECLARE
  value record;
BEGIN
  FOR value IN
    SELECT * FROM memory.plan_owner_v5_2_local_packet_route_v1(25)
  LOOP
    IF value.route NOT IN (
         'terminal_no_stage','manual_review_artifact_ready'
       )
       OR value.reason_code NOT IN (
         'deferral_only_no_stage_v5_2',
         'reviewable_relational_packet_v5_2'
       )
       OR value.routing_basis_sha256 !~ '^[0-9a-f]{64}$'
       OR value.entity_mention_count NOT BETWEEN 0 AND 24
       OR value.observation_count NOT BETWEEN 0 AND 32
       OR value.comparison_hint_count NOT BETWEEN 0 AND 32
       OR value.deferral_count NOT BETWEEN 0 AND 32 THEN
      RAISE EXCEPTION 'V5.2 planner returned an invalid row';
    END IF;
  END LOOP;
END
$owner_plan$;

SELECT set_config('test.target_packet_id', :'target_packet_id', true);

SELECT set_config(
  'app.user_id',
  '557ea042-cb82-48f8-9429-472e96c957ef',
  true
);

DO $cross_owner$
DECLARE
  target uuid := current_setting('test.target_packet_id')::uuid;
  leaked boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_2_local_packet_route_v1(25)
    WHERE packet_id=target
  ) INTO leaked;
  IF leaked THEN
    RAISE EXCEPTION 'V5.2 route planner leaked another owner';
  END IF;
END
$cross_owner$;

RESET ROLE;

ROLLBACK;

SELECT 'memory_v1_v5_2_local_packet_router_security: PASS' AS result;
