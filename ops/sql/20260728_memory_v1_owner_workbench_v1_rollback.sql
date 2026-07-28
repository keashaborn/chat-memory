BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DROP TRIGGER IF EXISTS owner_packet_feedback_atom_guard
  ON memory.v5_2_atom_admission_proposal;
DROP TRIGGER IF EXISTS owner_packet_feedback_stage_guard
  ON memory.v5_local_packet_stage_admission;
DROP FUNCTION IF EXISTS memory.guard_owner_packet_feedback_promotion_v1();
DROP FUNCTION IF EXISTS memory.record_owner_memory_workbench_feedback_v1(
  uuid,uuid,text,text,text
);
DROP FUNCTION IF EXISTS memory.list_owner_memory_workbench_v1(
  text,integer,timestamptz,uuid
);
DROP FUNCTION IF EXISTS memory.owner_memory_workbench_summary_v1();
DROP TABLE IF EXISTS memory.owner_packet_feedback_v1;

COMMIT;
