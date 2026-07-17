BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5 project component rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.project_component_registration_event_v5 LIMIT 1
  ) OR EXISTS (
    SELECT 1 FROM memory.project_component_alias_v5 LIMIT 1
  ) OR EXISTS (
    SELECT 1 FROM memory.project_component_v5 LIMIT 1
  ) THEN
    RAISE EXCEPTION 'refusing to remove non-empty V5 project component tables';
  END IF;
END
$preflight$;

DROP FUNCTION IF EXISTS memory.read_owner_project_components_v5(uuid);
DROP FUNCTION IF EXISTS memory.apply_owner_project_component_v5(
  uuid,uuid,text,text,uuid,text[],jsonb
);
DROP FUNCTION IF EXISTS memory.normalize_project_component_alias_v5(text);

DROP TABLE IF EXISTS memory.project_component_registration_event_v5;
DROP TABLE IF EXISTS memory.project_component_alias_v5;
DROP TABLE IF EXISTS memory.project_component_v5;

COMMIT;
