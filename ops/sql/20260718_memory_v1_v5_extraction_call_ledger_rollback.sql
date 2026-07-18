BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 extraction call ledger rollback requires sage';
  END IF;
  IF to_regclass('memory.v5_extraction_call_event') IS NOT NULL
     AND EXISTS (SELECT 1 FROM memory.v5_extraction_call_event LIMIT 1) THEN
    RAISE EXCEPTION 'refusing to drop nonempty V5 extraction call ledger';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.complete_owner_v5_extraction_call_v1(
  uuid,uuid,uuid,uuid,text,integer,text,text,text,text
);
DROP FUNCTION IF EXISTS memory.claim_owner_v5_bounded_extraction_job_v1(
  uuid,uuid,text,text,integer,integer,text,text,text,integer,integer,integer
);
DROP TABLE IF EXISTS memory.v5_extraction_call_event;
DROP FUNCTION IF EXISTS memory.guard_v5_extraction_call_append_only();
REVOKE USAGE ON SCHEMA memory
  FROM memory_v5_extraction_scheduler_maintainer;
REVOKE EXECUTE ON FUNCTION memory.current_actor_user_id()
  FROM memory_v5_extraction_scheduler_maintainer;
REVOKE EXECUTE ON FUNCTION public.digest(bytea,text)
  FROM memory_v5_extraction_scheduler_maintainer;
REVOKE SELECT,UPDATE ON memory.evidence_extraction_job
  FROM memory_v5_extraction_scheduler_maintainer;
REVOKE SELECT,INSERT ON memory.evidence_extraction_event
  FROM memory_v5_extraction_scheduler_maintainer;
REVOKE SELECT ON memory.evidence
  FROM memory_v5_extraction_scheduler_maintainer;
REVOKE SELECT ON memory.evidence_extraction_packet_v5
  FROM memory_v5_extraction_scheduler_maintainer;
DROP ROLE IF EXISTS memory_v5_extraction_scheduler_maintainer;

COMMIT;
