BEGIN;

DO $guard$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.projection_plan
    WHERE projector='memory_v1_deterministic_project_projection_v5'
      AND projector_version='project_current_state_v1'
  ) THEN
    RAISE EXCEPTION 'project projection API has durable plans; rollback refused';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.stage_project_projection_plan_v5(
  uuid,text,text
);
DROP FUNCTION IF EXISTS memory.preflight_project_projection_packet_v5(
  uuid,text
);
DROP FUNCTION IF EXISTS memory.preflight_project_projection_source_v5(uuid);

COMMIT;
