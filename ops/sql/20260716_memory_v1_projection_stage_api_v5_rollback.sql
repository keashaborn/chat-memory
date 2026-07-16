BEGIN;
DO $guard$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'projection stage API rollback requires sage';
  END IF;
  IF EXISTS (SELECT 1 FROM memory.projection_plan) THEN
    RAISE EXCEPTION 'projection stage API rollback blocked by staged plans';
  END IF;
END
$guard$;
DROP FUNCTION IF EXISTS memory.stage_projection_plan_v5(uuid,text,text);
COMMIT;
