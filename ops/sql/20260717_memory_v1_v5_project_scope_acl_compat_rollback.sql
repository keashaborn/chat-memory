BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5 project scope ACL rollback requires sage';
  END IF;
  IF EXISTS (SELECT 1 FROM memory.project_component_v5)
     OR EXISTS (SELECT 1 FROM memory.project_component_alias_v5)
     OR EXISTS (
       SELECT 1 FROM memory.project_component_registration_event_v5
     ) THEN
    RAISE EXCEPTION 'refusing ACL rollback after component registration';
  END IF;
END
$preflight$;

REVOKE EXECUTE ON FUNCTION memory.v5_project_scope_valid(jsonb)
  FROM memory_v5_extraction_maintainer;

COMMIT;
