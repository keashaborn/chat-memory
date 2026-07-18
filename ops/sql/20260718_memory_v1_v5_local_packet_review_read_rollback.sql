BEGIN;

DROP FUNCTION IF EXISTS memory.read_owner_v5_local_packet_review_v1(uuid);

REVOKE USAGE ON SCHEMA memory FROM memory_v5_local_review_reader;
REVOKE EXECUTE ON FUNCTION memory.current_actor_user_id()
  FROM memory_v5_local_review_reader;
REVOKE EXECUTE ON FUNCTION public.digest(bytea,text)
  FROM memory_v5_local_review_reader;
REVOKE SELECT ON
  memory.evidence_extraction_packet_v5_local,
  memory.evidence_extraction_job,
  memory.evidence,
  memory.relational_stage_batch
FROM memory_v5_local_review_reader;

DO $role$
BEGIN
  IF to_regrole('memory_v5_local_review_reader') IS NOT NULL THEN
    DROP ROLE memory_v5_local_review_reader;
  END IF;
END
$role$;

COMMIT;
