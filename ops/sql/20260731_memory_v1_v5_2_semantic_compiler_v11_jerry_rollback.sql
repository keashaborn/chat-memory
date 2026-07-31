BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DROP POLICY IF EXISTS v5_2_compiler_v11_jerry_supersession_route_read
  ON memory.v5_2_local_packet_route_event;
DROP FUNCTION IF EXISTS
memory.finalize_owner_v5_2_semantic_compiler_v11_jerry_supersession_v1(
  uuid,uuid,uuid,uuid,text,text,text,text
);
DROP FUNCTION IF EXISTS
memory.plan_owner_v5_2_semantic_compiler_v11_jerry_supersession_v1(uuid,uuid);
DROP FUNCTION IF EXISTS
memory.enqueue_owner_v5_2_semantic_compiler_v11_jerry_v1(jsonb,text,text);

ALTER TABLE memory.v5_local_packet_supersession
  DROP CONSTRAINT v5_local_packet_supersession_reason_code_check;
ALTER TABLE memory.v5_local_packet_supersession
  ADD CONSTRAINT v5_local_packet_supersession_reason_code_check
  CHECK (reason_code IN (
    'temporal_persistence_matrix_reextracted',
    'pet_identity_semantics_reextracted',
    'duplicate_active_packet_reconciled',
    'semantic_compiler_v10_reextracted'
  ));

COMMIT;
