BEGIN;
DROP FUNCTION IF EXISTS
memory.finalize_owner_v5_2_pet_identity_packet_supersession_v1(
  uuid,uuid,uuid,uuid,text,text,text
);
DROP FUNCTION IF EXISTS
memory.plan_owner_v5_2_pet_identity_packet_supersession_v1(uuid,uuid);
ALTER TABLE memory.v5_local_packet_supersession
  DROP CONSTRAINT v5_local_packet_supersession_reason_code_check;
ALTER TABLE memory.v5_local_packet_supersession
  ADD CONSTRAINT v5_local_packet_supersession_reason_code_check
  CHECK (reason_code='temporal_persistence_matrix_reextracted');
COMMIT;
