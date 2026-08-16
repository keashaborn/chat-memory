\set ON_ERROR_STOP on

BEGIN;
SET ROLE lifeswitch_owner;

DO $disable_owner_rls$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'lifeswitch_nutrition.meal'::regclass,
    'lifeswitch_nutrition.meal_item'::regclass,
    'lifeswitch_nutrition.meal_plan'::regclass,
    'lifeswitch_nutrition.meal_plan_item'::regclass,
    'lifeswitch_nutrition.my_food'::regclass,
    'lifeswitch_nutrition.my_food_override'::regclass,
    'lifeswitch_nutrition.my_food_serving'::regclass,
    'lifeswitch_nutrition.nutrition_day'::regclass,
    'lifeswitch_nutrition.nutrition_day_completion_event'::regclass,
    'lifeswitch_nutrition.nutrition_entry'::regclass,
    'lifeswitch_training.conditioning_session_log'::regclass,
    'lifeswitch_training.my_conditioning_prescription'::regclass,
    'lifeswitch_training.my_exercise'::regclass,
    'lifeswitch_training.my_exercise_role_event'::regclass,
    'lifeswitch_training.training_observation_event'::regclass,
    'lifeswitch_training.training_session'::regclass,
    'lifeswitch_training.training_session_role_event'::regclass,
    'lifeswitch_training.training_set_log'::regclass,
    'lifeswitch_training.training_set_log_segment'::regclass,
    'lifeswitch_training.workout_template'::regclass,
    'lifeswitch_training.workout_template_exercise'::regclass,
    'lifeswitch_training.workout_template_exercise_segment'::regclass,
    'lifeswitch_training.workout_template_role_event'::regclass,
    'lifeswitch_training.workout_template_share'::regclass,
    'public.lifeswitch_measurement_entries'::regclass
  ] LOOP
    EXECUTE format('ALTER TABLE %s NO FORCE ROW LEVEL SECURITY', target);
    EXECUTE format('ALTER TABLE %s DISABLE ROW LEVEL SECURITY', target);
    EXECUTE format('DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON %s', target);
  END LOOP;
END
$disable_owner_rls$;

GRANT SELECT ON ALL TABLES IN SCHEMA lifeswitch_snapshot TO lifeswitch_app;

ALTER VIEW lifeswitch_training.conditioning_session_current_v
  SET (security_invoker = false);
ALTER VIEW lifeswitch_training.training_session_current_v
  SET (security_invoker = false);
ALTER VIEW lifeswitch_training.training_set_effective_role_v1
  SET (security_invoker = false);

RESET ROLE;
COMMIT;
