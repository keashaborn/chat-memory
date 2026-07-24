BEGIN;

DROP TRIGGER IF EXISTS v5_2_terminal_evidence_stage_guard
  ON memory.relational_stage_batch;
ALTER POLICY owner_isolation ON memory.relational_stage_batch
  TO memory_v5_writer,memory_v5_local_disposition_maintainer,
     memory_v5_local_review_reader;
DROP FUNCTION IF EXISTS memory.guard_v5_2_terminal_evidence_from_stage_v1();
DROP FUNCTION IF EXISTS memory.record_owner_v5_2_review_route_v1(
  uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
);
DROP FUNCTION IF EXISTS memory.finalize_owner_v5_2_terminal_route_v1(
  uuid,uuid,uuid,text,text,text[]
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_2_local_packet_route_v1(integer);
DROP TABLE IF EXISTS memory.v5_2_local_packet_route_event;
DROP ROLE IF EXISTS memory_v5_2_local_router_maintainer;

COMMIT;
