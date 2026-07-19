BEGIN;
DROP FUNCTION IF EXISTS
  memory.enqueue_owner_v5_local_review_refresh_reextract_v1(
    uuid,uuid,uuid,uuid,text,text,text,text,text,text
  );
DROP FUNCTION IF EXISTS
  memory.plan_owner_v5_local_review_refresh_reextract_v1(uuid,text,text);
COMMIT;
