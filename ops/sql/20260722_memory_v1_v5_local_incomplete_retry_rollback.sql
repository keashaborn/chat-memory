BEGIN;
DROP FUNCTION IF EXISTS memory.requeue_owner_local_incomplete_failure_v1(
  uuid,uuid,text,uuid,uuid,integer,text
);
COMMIT;
