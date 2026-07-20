BEGIN;
DROP FUNCTION IF EXISTS memory.enqueue_owner_v5_local_semantic_reextract_v3(
  uuid,uuid,uuid,uuid,text,text,text,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_local_semantic_reextract_v3(uuid);
COMMIT;
