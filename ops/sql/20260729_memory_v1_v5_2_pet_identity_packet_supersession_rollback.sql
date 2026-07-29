BEGIN;
DROP FUNCTION IF EXISTS
memory.finalize_owner_v5_2_pet_identity_packet_supersession_v1(
  uuid,uuid,uuid,uuid,text,text,text
);
DROP FUNCTION IF EXISTS
memory.plan_owner_v5_2_pet_identity_packet_supersession_v1(uuid,uuid);
COMMIT;
