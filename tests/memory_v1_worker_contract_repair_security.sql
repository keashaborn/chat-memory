\set ON_ERROR_STOP on
BEGIN;
SET LOCAL statement_timeout='30s';

DO $catalog$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace
    WHERE namespace.nspname='memory'
      AND relation.relname='v5_local_claim_projection_terminal_v1'
      AND relation.relrowsecurity
      AND relation.relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'claim projection terminal table is not forced-RLS';
  END IF;
  IF pg_get_userbyid((
       SELECT proowner
       FROM pg_proc
       WHERE oid=
         'memory.inspect_existing_claim_projection_v1(uuid,text)'::regprocedure
     ))<>'memory_v5_writer' THEN
    RAISE EXCEPTION 'existing-claim inspector owner is incorrect';
  END IF;
  IF pg_get_userbyid((
       SELECT proowner
     FROM pg_proc
       WHERE oid=
         'memory.register_owner_v5_local_claim_projection_v2(uuid,uuid,uuid,uuid,text,text,text,text)'::regprocedure
     ))<>'memory_v5_writer' THEN
    RAISE EXCEPTION 'claim projection v2 register owner is incorrect';
  END IF;
  IF pg_get_userbyid((
       SELECT proowner
       FROM pg_proc
       WHERE oid=
         'memory.local_claim_projection_source_accepted_v1(uuid,uuid)'::regprocedure
     ))<>'memory_v5_local_projection_maintainer' THEN
    RAISE EXCEPTION 'claim source-check owner is incorrect';
  END IF;
  IF has_table_privilege(
       'brains_app','memory.v5_local_claim_projection_terminal_v1','SELECT'
     )
     OR has_table_privilege(
       'brains_app','memory.v5_local_claim_projection_terminal_v1','INSERT'
     )
     OR has_function_privilege(
       'brains_app',
       'memory.inspect_existing_claim_projection_v1(uuid,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app received direct terminal/claim inspection access';
  END IF;
  IF NOT has_function_privilege(
       'brains_app',
       'memory.register_owner_v5_local_claim_projection_v2(uuid,uuid,uuid,uuid,text,text,text,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app lacks the restricted v2 register capability';
  END IF;
  IF position(
       'packet.local_model_calls BETWEEN 0 AND 1'
       IN pg_get_functiondef(
         'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure
       )
     )=0 THEN
    RAISE EXCEPTION 'exact packet planner is not zero-call compatible';
  END IF;
  IF position(
       'memory.v5_local_claim_projection_terminal_v1'
       IN pg_get_functiondef(
         'memory.plan_owner_v5_local_claim_projection_v1(integer)'::regprocedure
       )
     )=0 THEN
    RAISE EXCEPTION 'claim planner does not exclude terminal outcomes';
  END IF;
END
$catalog$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_user_id',true);
SELECT set_config('test.packet_id',:'packet_id',true);
DO $packet_equivalence$
DECLARE
  global_packet uuid;
  exact_packet uuid;
BEGIN
  SELECT packet_id INTO STRICT global_packet
  FROM memory.plan_owner_v5_2_local_packet_route_v1(1);
  IF global_packet<>current_setting('test.packet_id')::uuid THEN
    RAISE EXCEPTION 'unexpected global packet target';
  END IF;
  SELECT packet_id INTO STRICT exact_packet
  FROM memory.plan_owner_v5_2_exact_packet_route_v1(
    current_setting('test.packet_id')::uuid
  );
  IF exact_packet<>global_packet THEN
    RAISE EXCEPTION 'global and exact packet planners disagree';
  END IF;
END
$packet_equivalence$;
RESET SESSION AUTHORIZATION;

ROLLBACK;
SELECT 'memory_v1_worker_contract_repair_security: PASS' AS result;
