\set ON_ERROR_STOP on

BEGIN;
SET ROLE lifeswitch_owner;

-- Direct owner tables.
DO $direct_owner_rls$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'lifeswitch_nutrition.meal'::regclass,
    'lifeswitch_nutrition.meal_plan'::regclass,
    'lifeswitch_nutrition.my_food'::regclass,
    'lifeswitch_nutrition.nutrition_day'::regclass,
    'lifeswitch_training.my_conditioning_prescription'::regclass,
    'lifeswitch_training.my_exercise'::regclass,
    'lifeswitch_training.training_observation_event'::regclass,
    'lifeswitch_training.training_session'::regclass,
    'lifeswitch_training.workout_template'::regclass
  ] LOOP
    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', target);
    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', target);
    EXECUTE format('DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON %s', target);
    EXECUTE format(
      'CREATE POLICY lifeswitch_owner_isolation_v1 ON %s '
      'FOR ALL TO lifeswitch_app, lifeswitch_owner '
      'USING (owner_user_id = NULLIF(current_setting(''app.user_id'', true), '''')::uuid) '
      'WITH CHECK (owner_user_id = NULLIF(current_setting(''app.user_id'', true), '''')::uuid)',
      target
    );
  END LOOP;
END
$direct_owner_rls$;

-- Measurements predates the UUID-typed domain schemas and stores its owner as text.
ALTER TABLE public.lifeswitch_measurement_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lifeswitch_measurement_entries FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON public.lifeswitch_measurement_entries;
CREATE POLICY lifeswitch_owner_isolation_v1
ON public.lifeswitch_measurement_entries
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')
)
WITH CHECK (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')
);

-- Nutrition child rows must resolve through an owner-visible parent.
ALTER TABLE lifeswitch_nutrition.meal_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_nutrition.meal_item FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON lifeswitch_nutrition.meal_item;
CREATE POLICY lifeswitch_owner_isolation_v1
ON lifeswitch_nutrition.meal_item
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.meal parent
    WHERE parent.meal_id = meal_item.meal_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
  AND EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.my_food food
    WHERE food.my_food_id = meal_item.my_food_id
      AND food.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.meal parent
    WHERE parent.meal_id = meal_item.meal_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
  AND EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.my_food food
    WHERE food.my_food_id = meal_item.my_food_id
      AND food.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
);

ALTER TABLE lifeswitch_nutrition.meal_plan_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_nutrition.meal_plan_item FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON lifeswitch_nutrition.meal_plan_item;
CREATE POLICY lifeswitch_owner_isolation_v1
ON lifeswitch_nutrition.meal_plan_item
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.meal_plan parent
    WHERE parent.meal_plan_id = meal_plan_item.meal_plan_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
  AND (
    meal_plan_item.my_food_id IS NULL OR EXISTS (
      SELECT 1 FROM lifeswitch_nutrition.my_food food
      WHERE food.my_food_id = meal_plan_item.my_food_id
        AND food.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
    )
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.meal_plan parent
    WHERE parent.meal_plan_id = meal_plan_item.meal_plan_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
  AND (
    meal_plan_item.my_food_id IS NULL OR EXISTS (
      SELECT 1 FROM lifeswitch_nutrition.my_food food
      WHERE food.my_food_id = meal_plan_item.my_food_id
        AND food.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
    )
  )
);

ALTER TABLE lifeswitch_nutrition.my_food_serving ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_nutrition.my_food_serving FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON lifeswitch_nutrition.my_food_serving;
CREATE POLICY lifeswitch_owner_isolation_v1
ON lifeswitch_nutrition.my_food_serving
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.my_food parent
    WHERE parent.my_food_id = my_food_serving.my_food_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.my_food parent
    WHERE parent.my_food_id = my_food_serving.my_food_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
);

ALTER TABLE lifeswitch_nutrition.my_food_override ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_nutrition.my_food_override FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON lifeswitch_nutrition.my_food_override;
CREATE POLICY lifeswitch_owner_isolation_v1
ON lifeswitch_nutrition.my_food_override
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  AND EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.my_food parent
    WHERE parent.my_food_id = my_food_override.my_food_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
)
WITH CHECK (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  AND EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.my_food parent
    WHERE parent.my_food_id = my_food_override.my_food_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
);

ALTER TABLE lifeswitch_nutrition.nutrition_day_completion_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_nutrition.nutrition_day_completion_event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON lifeswitch_nutrition.nutrition_day_completion_event;
CREATE POLICY lifeswitch_owner_isolation_v1
ON lifeswitch_nutrition.nutrition_day_completion_event
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  AND EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.nutrition_day parent
    WHERE parent.nutrition_day_id = nutrition_day_completion_event.nutrition_day_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
)
WITH CHECK (
  owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  AND EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.nutrition_day parent
    WHERE parent.nutrition_day_id = nutrition_day_completion_event.nutrition_day_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
);

ALTER TABLE lifeswitch_nutrition.nutrition_entry ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_nutrition.nutrition_entry FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON lifeswitch_nutrition.nutrition_entry;
CREATE POLICY lifeswitch_owner_isolation_v1
ON lifeswitch_nutrition.nutrition_entry
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.nutrition_day parent
    WHERE parent.nutrition_day_id = nutrition_entry.nutrition_day_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
  AND (
    nutrition_entry.meal_id IS NULL OR EXISTS (
      SELECT 1 FROM lifeswitch_nutrition.meal meal
      WHERE meal.meal_id = nutrition_entry.meal_id
        AND meal.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
    )
  )
  AND (
    nutrition_entry.my_food_id IS NULL OR EXISTS (
      SELECT 1 FROM lifeswitch_nutrition.my_food food
      WHERE food.my_food_id = nutrition_entry.my_food_id
        AND food.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
    )
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1 FROM lifeswitch_nutrition.nutrition_day parent
    WHERE parent.nutrition_day_id = nutrition_entry.nutrition_day_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
  AND (
    nutrition_entry.meal_id IS NULL OR EXISTS (
      SELECT 1 FROM lifeswitch_nutrition.meal meal
      WHERE meal.meal_id = nutrition_entry.meal_id
        AND meal.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
    )
  )
  AND (
    nutrition_entry.my_food_id IS NULL OR EXISTS (
      SELECT 1 FROM lifeswitch_nutrition.my_food food
      WHERE food.my_food_id = nutrition_entry.my_food_id
        AND food.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
    )
  )
);

-- Training tables with an explicit owner column and linked-parent consistency.
DO $training_owner_rls$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'lifeswitch_training.conditioning_session_log'::regclass,
    'lifeswitch_training.my_exercise_role_event'::regclass,
    'lifeswitch_training.training_session_role_event'::regclass,
    'lifeswitch_training.training_set_log'::regclass,
    'lifeswitch_training.training_set_log_segment'::regclass,
    'lifeswitch_training.workout_template_role_event'::regclass
  ] LOOP
    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', target);
    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', target);
    EXECUTE format('DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON %s', target);
    EXECUTE format(
      'CREATE POLICY lifeswitch_owner_isolation_v1 ON %s '
      'FOR ALL TO lifeswitch_app, lifeswitch_owner '
      'USING (owner_user_id = NULLIF(current_setting(''app.user_id'', true), '''')::uuid) '
      'WITH CHECK (owner_user_id = NULLIF(current_setting(''app.user_id'', true), '''')::uuid)',
      target
    );
  END LOOP;
END
$training_owner_rls$;

ALTER TABLE lifeswitch_training.workout_template_exercise ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_training.workout_template_exercise FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON lifeswitch_training.workout_template_exercise;
CREATE POLICY lifeswitch_owner_isolation_v1
ON lifeswitch_training.workout_template_exercise
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  EXISTS (
    SELECT 1 FROM lifeswitch_training.workout_template parent
    WHERE parent.workout_template_id = workout_template_exercise.workout_template_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1 FROM lifeswitch_training.workout_template parent
    WHERE parent.workout_template_id = workout_template_exercise.workout_template_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
);

ALTER TABLE lifeswitch_training.workout_template_exercise_segment ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_training.workout_template_exercise_segment FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON lifeswitch_training.workout_template_exercise_segment;
CREATE POLICY lifeswitch_owner_isolation_v1
ON lifeswitch_training.workout_template_exercise_segment
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  EXISTS (
    SELECT 1
    FROM lifeswitch_training.workout_template_exercise exercise
    JOIN lifeswitch_training.workout_template parent
      ON parent.workout_template_id = exercise.workout_template_id
    WHERE exercise.workout_template_exercise_id = workout_template_exercise_segment.workout_template_exercise_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM lifeswitch_training.workout_template_exercise exercise
    JOIN lifeswitch_training.workout_template parent
      ON parent.workout_template_id = exercise.workout_template_id
    WHERE exercise.workout_template_exercise_id = workout_template_exercise_segment.workout_template_exercise_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
);

ALTER TABLE lifeswitch_training.workout_template_share ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_training.workout_template_share FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lifeswitch_owner_isolation_v1 ON lifeswitch_training.workout_template_share;
CREATE POLICY lifeswitch_owner_isolation_v1
ON lifeswitch_training.workout_template_share
FOR ALL TO lifeswitch_app, lifeswitch_owner
USING (
  created_by_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  AND EXISTS (
    SELECT 1 FROM lifeswitch_training.workout_template parent
    WHERE parent.workout_template_id = workout_template_share.workout_template_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
)
WITH CHECK (
  created_by_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  AND EXISTS (
    SELECT 1 FROM lifeswitch_training.workout_template parent
    WHERE parent.workout_template_id = workout_template_share.workout_template_id
      AND parent.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
);

-- Preservation snapshots remain retained but are not a runtime read surface.
REVOKE ALL ON ALL TABLES IN SCHEMA lifeswitch_snapshot FROM lifeswitch_app;

-- Owner-bearing views must evaluate RLS as the application caller, not the
-- view owner. The app already has SELECT on every referenced training table.
ALTER VIEW lifeswitch_training.conditioning_session_current_v
  SET (security_invoker = true);
ALTER VIEW lifeswitch_training.training_session_current_v
  SET (security_invoker = true);
ALTER VIEW lifeswitch_training.training_set_effective_role_v1
  SET (security_invoker = true);

RESET ROLE;
COMMIT;
