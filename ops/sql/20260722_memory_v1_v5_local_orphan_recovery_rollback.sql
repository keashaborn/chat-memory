BEGIN;
DROP FUNCTION IF EXISTS memory.recover_owner_v5_local_orphan_v1(
  uuid,uuid,uuid,uuid,text
);
COMMIT;
