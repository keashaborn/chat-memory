BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 project shadow read rollback requires sage';
  END IF;
  IF to_regclass('memory.project_thread_component_binding_event_v5') IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM memory.project_thread_component_binding_event_v5 LIMIT 1
     ) THEN
    RAISE EXCEPTION 'component binding rows exist; automatic rollback is unsafe';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.read_v5_shadow_project_knowledge(uuid,integer);
DROP FUNCTION IF EXISTS memory.apply_owner_project_thread_component_binding_v5(
  uuid,uuid,uuid,uuid,uuid,uuid,text,text,text
);
DROP VIEW IF EXISTS memory.current_project_thread_component_binding_v5;
DROP TABLE IF EXISTS memory.project_thread_component_binding_event_v5;
DROP FUNCTION IF EXISTS memory.guard_project_component_binding_append_only_v5();

DROP POLICY IF EXISTS owner_isolation_v5_project_reader
  ON memory.project_knowledge_head_v5;
DROP POLICY IF EXISTS owner_isolation_v5_project_reader
  ON memory.project_knowledge_revision_v5;
DROP POLICY IF EXISTS owner_isolation_v5_project_reader
  ON memory.project_knowledge_revision_observation;

REVOKE SELECT ON
  memory.project_space,
  memory.project_component_v5,
  memory.project_thread_binding_event,
  memory.current_project_thread_binding_v5,
  memory.project_knowledge_head_v5,
  memory.project_knowledge_revision_v5,
  memory.project_knowledge_revision_observation
FROM memory_v5_reader;

REVOKE SELECT ON public.threads FROM memory_v5_reader;

COMMIT;
