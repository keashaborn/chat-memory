BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 bounded extraction rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.evidence_extraction_packet_v5 LIMIT 1
  ) OR EXISTS (
    SELECT 1 FROM memory.project_thread_binding_event LIMIT 1
  ) THEN
    RAISE EXCEPTION 'V5 bounded extraction rollback refuses non-empty tables';
  END IF;
END
$preflight$;

DROP FUNCTION IF EXISTS memory.persist_owner_evidence_extraction_packet_v5(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,jsonb,boolean,integer,uuid
);
DROP FUNCTION IF EXISTS memory.read_owner_evidence_extraction_context_v5(
  uuid,uuid,text,text
);
DROP FUNCTION IF EXISTS memory.apply_owner_project_thread_binding_v5(
  uuid,uuid,uuid,text,text
);
DROP VIEW IF EXISTS memory.current_project_thread_binding_v5;
DROP TABLE IF EXISTS memory.evidence_extraction_packet_v5;
DROP TABLE IF EXISTS memory.project_thread_binding_event;
DROP FUNCTION IF EXISTS memory.guard_v5_extraction_append_only();

REVOKE USAGE ON SCHEMA memory
  FROM memory_v5_extraction_maintainer;
REVOKE EXECUTE ON FUNCTION memory.current_actor_user_id()
  FROM memory_v5_extraction_maintainer;
REVOKE EXECUTE ON FUNCTION public.digest(bytea,text)
  FROM memory_v5_extraction_maintainer;
REVOKE ALL ON
  public.threads,
  memory.project_space,
  memory.evidence,
  memory.evidence_extraction_job,
  memory.evidence_extraction_event
FROM memory_v5_extraction_maintainer;

DO $role$
BEGIN
  IF to_regrole('memory_v5_extraction_maintainer') IS NOT NULL THEN
    DROP ROLE memory_v5_extraction_maintainer;
  END IF;
END
$role$;

COMMIT;
