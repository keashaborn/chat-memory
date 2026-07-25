BEGIN;

DO $guard$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.projection_plan_item
    WHERE target_reason_codes
      @> '["additional_supporting_observation"]'::jsonb
  ) THEN
    RAISE EXCEPTION
      'cannot roll back V5.2 reinforcement functions while rows exist';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS
  memory.stage_projection_reinforcement_v5_2(uuid,text,text);
DROP FUNCTION IF EXISTS
  memory.preflight_projection_reinforcement_v5_2(uuid,text);

COMMIT;
