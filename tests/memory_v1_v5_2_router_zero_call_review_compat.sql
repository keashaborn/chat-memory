\set ON_ERROR_STOP on

SELECT set_config('test.owner_user_id', :'owner_user_id', false);
SELECT set_config('test.other_owner_user_id', :'other_owner_user_id', false);
SELECT set_config('test.zero_call_packet_id', :'zero_call_packet_id', false);
SELECT set_config('test.one_call_packet_id', :'one_call_packet_id', false);

DO $catalog$
DECLARE
  function_definition text;
BEGIN
  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_2_local_packet_route_v1(integer)'::regprocedure
  ) INTO function_definition;
  IF strpos(
       function_definition,
       'packet.local_model_calls BETWEEN 0 AND 1'
     )=0
     OR regexp_count(
       function_definition,
       'packet\.local_model_calls\s*=\s*1'
     )<>0 THEN
    RAISE EXCEPTION 'V5.2 planner is not zero-call compatible';
  END IF;
  IF NOT has_function_privilege(
       'brains_app',
       'memory.plan_owner_v5_2_local_packet_route_v1(integer)',
       'EXECUTE'
     )
     OR has_table_privilege(
       'brains_app','memory.v5_2_local_packet_route_event','INSERT'
     ) THEN
    RAISE EXCEPTION 'V5.2 router ACL is unsafe';
  END IF;
END
$catalog$;

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner_user_id', true);

DO $owner_visibility$
BEGIN
  IF (
    SELECT count(*)
    FROM memory.plan_owner_v5_2_local_packet_route_v1(25)
    WHERE packet_id IN (
      current_setting('test.zero_call_packet_id')::uuid,
      current_setting('test.one_call_packet_id')::uuid
    )
      AND route='manual_review_artifact_ready'
  )<>2 THEN
    RAISE EXCEPTION 'exact zero/one-call review packets are not both visible';
  END IF;
END
$owner_visibility$;

SELECT set_config('app.user_id', :'other_owner_user_id', true);
DO $isolation$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_2_local_packet_route_v1(25)
    WHERE packet_id IN (
      current_setting('test.zero_call_packet_id')::uuid,
      current_setting('test.one_call_packet_id')::uuid
    )
  ) THEN
    RAISE EXCEPTION 'V5.2 route plan crossed owner boundary';
  END IF;
END
$isolation$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_2_router_zero_call_review_compat: PASS' AS result;
