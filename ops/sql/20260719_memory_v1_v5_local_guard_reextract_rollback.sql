BEGIN;
DROP FUNCTION IF EXISTS memory.enqueue_owner_v5_local_guard_reextract_v1(
  uuid,uuid,uuid,uuid,text,text,text,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_local_guard_reextract_v1(uuid);
DO $cleanup$
BEGIN
  IF to_regrole('memory_v5_local_reextract_maintainer') IS NOT NULL THEN
    REVOKE ALL ON SCHEMA memory FROM memory_v5_local_reextract_maintainer;
    REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA memory
      FROM memory_v5_local_reextract_maintainer;
    REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA memory
      FROM memory_v5_local_reextract_maintainer;
    DROP ROLE memory_v5_local_reextract_maintainer;
  END IF;
END
$cleanup$;
COMMIT;
