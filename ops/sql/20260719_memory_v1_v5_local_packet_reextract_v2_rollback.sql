BEGIN;
DROP FUNCTION IF EXISTS memory.enqueue_owner_v5_local_packet_reextract_v2(
  uuid,uuid,uuid,uuid,text,text,text,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_local_packet_reextract_v2(uuid);
COMMIT;
