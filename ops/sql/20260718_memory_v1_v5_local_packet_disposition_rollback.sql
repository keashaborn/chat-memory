BEGIN;
SET LOCAL lock_timeout='5s';
DROP FUNCTION IF EXISTS memory.finalize_owner_v5_local_deferral_v1(
  uuid,uuid,uuid,text,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_local_packet_disposition_v1(integer);
DROP TABLE IF EXISTS memory.v5_local_packet_disposition;
DO $rollback$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='memory_v5_local_disposition_maintainer') THEN
    REVOKE SELECT ON memory.evidence_extraction_packet_v5_local,
      memory.evidence_extraction_job,memory.evidence,
      memory.relational_stage_batch
    FROM memory_v5_local_disposition_maintainer;
    REVOKE USAGE ON SCHEMA memory FROM memory_v5_local_disposition_maintainer;
    REVOKE EXECUTE ON FUNCTION memory.current_actor_user_id()
      FROM memory_v5_local_disposition_maintainer;
    REVOKE EXECUTE ON FUNCTION public.digest(bytea,text)
      FROM memory_v5_local_disposition_maintainer;
    DROP ROLE memory_v5_local_disposition_maintainer;
  END IF;
END
$rollback$;
COMMIT;
