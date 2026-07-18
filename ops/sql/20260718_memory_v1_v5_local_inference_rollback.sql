BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 local inference rollback requires sage';
  END IF;
END
$preflight$;

DROP FUNCTION IF EXISTS memory.complete_owner_v5_local_inference_v1(
  uuid,uuid,uuid,uuid,text,integer,text,text,text,text
);
DROP FUNCTION IF EXISTS memory.requeue_owner_local_transport_failure_v1(
  uuid,uuid,text,uuid,uuid,integer,text,text
);
DROP FUNCTION IF EXISTS memory.persist_owner_v5_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
);
DROP FUNCTION IF EXISTS memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
);

DROP TRIGGER IF EXISTS v5_local_packet_append_only_guard
  ON memory.evidence_extraction_packet_v5_local;
DROP TRIGGER IF EXISTS v5_local_inference_event_append_only_guard
  ON memory.v5_local_inference_event;
DROP TABLE IF EXISTS memory.evidence_extraction_packet_v5_local;
DROP TABLE IF EXISTS memory.v5_local_inference_event;
DROP FUNCTION IF EXISTS memory.guard_v5_local_inference_append_only();

DO $role$
BEGIN
  IF to_regrole('memory_v5_local_inference_maintainer') IS NOT NULL THEN
    REVOKE USAGE ON SCHEMA memory
      FROM memory_v5_local_inference_maintainer;
    REVOKE EXECUTE ON FUNCTION memory.current_actor_user_id()
      FROM memory_v5_local_inference_maintainer;
    REVOKE EXECUTE ON FUNCTION public.digest(bytea,text)
      FROM memory_v5_local_inference_maintainer;
    REVOKE EXECUTE ON FUNCTION memory.claim_owner_evidence_extraction_job_v1(
      uuid,text,text,integer,integer
    ) FROM memory_v5_local_inference_maintainer;
    REVOKE SELECT,UPDATE ON memory.evidence_extraction_job
      FROM memory_v5_local_inference_maintainer;
    REVOKE SELECT,INSERT ON memory.evidence_extraction_event
      FROM memory_v5_local_inference_maintainer;
    REVOKE SELECT ON memory.evidence
      FROM memory_v5_local_inference_maintainer;
    REVOKE SELECT ON memory.evidence_extraction_packet_v5
      FROM memory_v5_local_inference_maintainer;
    DROP ROLE memory_v5_local_inference_maintainer;
  END IF;
END
$role$;

COMMIT;
