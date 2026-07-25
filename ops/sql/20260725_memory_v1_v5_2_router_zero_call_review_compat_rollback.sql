BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $rollback$
DECLARE
  function_definition text;
BEGIN
  IF session_user<>'sage'
     OR to_regprocedure(
       'memory.plan_owner_v5_2_local_packet_route_v1(integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.2 zero-call review rollback prerequisites are absent';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.v5_2_local_packet_route_event AS route
    JOIN memory.evidence_extraction_packet_v5_local AS packet
      USING(owner_user_id,packet_id)
    WHERE packet.local_model_calls=0
  ) THEN
    RAISE EXCEPTION 'zero-call review routes prevent compatibility rollback';
  END IF;

  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_2_local_packet_route_v1(integer)'::regprocedure
  ) INTO function_definition;
  IF regexp_count(
       function_definition,
       'packet\.local_model_calls\s+BETWEEN\s+0\s+AND\s+1'
     )=1 THEN
    function_definition:=regexp_replace(
      function_definition,
      'packet\.local_model_calls\s+BETWEEN\s+0\s+AND\s+1',
      'packet.local_model_calls=1'
    );
    EXECUTE function_definition;
  ELSIF regexp_count(
    function_definition,
    'packet\.local_model_calls\s*=\s*1'
  )<>1 THEN
    RAISE EXCEPTION 'unexpected V5.2 planner rollback predicate';
  END IF;
END
$rollback$;

ALTER FUNCTION memory.plan_owner_v5_2_local_packet_route_v1(integer)
  OWNER TO memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_local_packet_route_v1(integer)
  FROM PUBLIC,brains_app,memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_local_packet_route_v1(integer)
  TO brains_app,memory_v5_2_local_router_maintainer;

COMMIT;
