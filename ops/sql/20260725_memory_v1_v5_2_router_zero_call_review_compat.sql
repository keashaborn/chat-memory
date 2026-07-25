BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $migration$
DECLARE
  function_definition text;
BEGIN
  IF session_user<>'sage'
     OR to_regprocedure(
       'memory.plan_owner_v5_2_local_packet_route_v1(integer)'
     ) IS NULL
     OR to_regprocedure(
       'memory.record_owner_v5_2_review_route_v1('
       'uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,'
       'integer,integer,integer,integer,integer)'
     ) IS NULL
     OR to_regrole('memory_v5_2_local_router_maintainer') IS NULL
     OR to_regrole('brains_app') IS NULL THEN
    RAISE EXCEPTION 'V5.2 zero-call review compatibility prerequisites are absent';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_packet_v5_local
    WHERE local_model_calls NOT BETWEEN 0 AND 1
       OR external_model_calls<>0
  ) THEN
    RAISE EXCEPTION 'existing local packet call provenance is invalid';
  END IF;

  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_2_local_packet_route_v1(integer)'::regprocedure
  ) INTO function_definition;
  IF regexp_count(
       function_definition,
       'packet\.local_model_calls\s*=\s*1'
     )=1 THEN
    function_definition:=regexp_replace(
      function_definition,
      'packet\.local_model_calls\s*=\s*1',
      'packet.local_model_calls BETWEEN 0 AND 1'
    );
    EXECUTE function_definition;
  ELSIF strpos(
    function_definition,
    'packet.local_model_calls BETWEEN 0 AND 1'
  )=0 THEN
    RAISE EXCEPTION 'unexpected V5.2 planner local-call predicate';
  END IF;
END
$migration$;

ALTER FUNCTION memory.plan_owner_v5_2_local_packet_route_v1(integer)
  OWNER TO memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_local_packet_route_v1(integer)
  FROM PUBLIC,brains_app,memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_local_packet_route_v1(integer)
  TO brains_app,memory_v5_2_local_router_maintainer;

COMMIT;
