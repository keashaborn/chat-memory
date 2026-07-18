BEGIN;

DO $rollback$
BEGIN
  IF to_regclass('memory.project_component_entity_binding_v5') IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM memory.project_component_entity_binding_v5 LIMIT 1
     ) THEN
    RAISE EXCEPTION 'project component entity bindings exist; rollback refused';
  END IF;
END
$rollback$;

DROP FUNCTION IF EXISTS memory.resolve_owner_project_component_entity_candidate_v5(
  text,text,text
);
DROP FUNCTION IF EXISTS memory.bootstrap_owner_project_component_entity_v5(
  uuid,uuid,uuid,text
);
DROP FUNCTION IF EXISTS memory.preflight_owner_project_component_entity_v5(
  uuid,uuid
);
DROP TABLE IF EXISTS memory.project_component_entity_binding_v5;

COMMIT;
