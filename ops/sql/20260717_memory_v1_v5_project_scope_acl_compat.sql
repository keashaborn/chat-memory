BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5 project scope ACL compatibility requires sage';
  END IF;
  IF to_regrole('memory_v5_extraction_maintainer') IS NULL
     OR to_regprocedure('memory.v5_project_scope_valid(jsonb)') IS NULL
     OR to_regprocedure(
       'memory.persist_owner_evidence_extraction_packet_v5(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,jsonb,boolean,integer,uuid)'
     ) IS NULL
     OR to_regclass('memory.project_component_v5') IS NULL
     OR to_regclass('memory.project_component_alias_v5') IS NULL
     OR to_regclass(
       'memory.project_component_registration_event_v5'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5 component/projection recovery prerequisites are absent';
  END IF;
  IF EXISTS (SELECT 1 FROM memory.project_component_v5)
     OR EXISTS (SELECT 1 FROM memory.project_component_alias_v5)
     OR EXISTS (
       SELECT 1 FROM memory.project_component_registration_event_v5
     )
     OR EXISTS (
       SELECT 1 FROM memory.project_knowledge_head_v5
       WHERE component_key IS NOT NULL
     )
     OR EXISTS (
       SELECT 1 FROM memory.projection_project_payload
       WHERE component_key IS NOT NULL
     ) THEN
    RAISE EXCEPTION
      'refusing pre-activation compatibility patch after component use';
  END IF;
END
$preflight$;

GRANT EXECUTE ON FUNCTION memory.v5_project_scope_valid(jsonb)
  TO memory_v5_extraction_maintainer;

COMMIT;
