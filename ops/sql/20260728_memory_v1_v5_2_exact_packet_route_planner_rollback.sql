\set ON_ERROR_STOP on

BEGIN;

DROP FUNCTION IF EXISTS
  memory.plan_owner_v5_2_exact_packet_route_v1(uuid);

COMMIT;
