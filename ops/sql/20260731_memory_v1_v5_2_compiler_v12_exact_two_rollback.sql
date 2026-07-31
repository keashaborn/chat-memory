BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DROP FUNCTION IF EXISTS
memory.enqueue_owner_v5_2_compiler_v12_exact_two_v1(jsonb,text,text);

DROP POLICY IF EXISTS v5_2_compiler_v12_exact_two_route_read
  ON memory.v5_2_local_packet_route_event;

COMMIT;
