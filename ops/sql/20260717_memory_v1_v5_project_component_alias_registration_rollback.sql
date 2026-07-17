BEGIN;

DO $rollback$
BEGIN
  IF to_regclass(
       'memory.project_component_alias_registration_event_v5'
     ) IS NOT NULL
     AND EXISTS (
       SELECT 1
       FROM memory.project_component_alias_registration_event_v5
     ) THEN
    RAISE EXCEPTION 'alias registration events exist; rollback refused';
  END IF;
END
$rollback$;

DROP FUNCTION IF EXISTS memory.apply_owner_project_component_alias_v5(
  uuid,uuid,uuid,text,jsonb
);
DROP TABLE IF EXISTS memory.project_component_alias_registration_event_v5;

COMMIT;
