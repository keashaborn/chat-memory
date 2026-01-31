BEGIN;

CREATE SCHEMA IF NOT EXISTS lifeswitch_training AUTHORIZATION sage;

-- match nutrition pattern
CREATE OR REPLACE FUNCTION lifeswitch_training.tg_set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

-- 1) My Exercises (DB-backed)
CREATE TABLE IF NOT EXISTS lifeswitch_training.my_exercise (
  my_exercise_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id  uuid NOT NULL,

  exercise_id    text NOT NULL,        -- canonical id from catalog_dev.exercise (or similar)
  display_name   text NOT NULL,
  kind           text NOT NULL,
  modality       text NOT NULL,
  brand_name     text,
  model_name     text,

  matched_text   text,
  matched_source text,

  is_active      boolean NOT NULL DEFAULT true,

  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),

  UNIQUE(owner_user_id, exercise_id)
);

DROP TRIGGER IF EXISTS trg_my_exercise_updated_at ON lifeswitch_training.my_exercise;
CREATE TRIGGER trg_my_exercise_updated_at
BEFORE UPDATE ON lifeswitch_training.my_exercise
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_my_exercise_owner ON lifeswitch_training.my_exercise(owner_user_id);
CREATE INDEX IF NOT EXISTS ix_my_exercise_owner_name ON lifeswitch_training.my_exercise(owner_user_id, lower(display_name));

-- 2) Workout templates (DB-backed)
CREATE TABLE IF NOT EXISTS lifeswitch_training.workout_template (
  workout_template_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id       uuid NOT NULL,

  name                text NOT NULL,
  notes               text,

  is_active           boolean NOT NULL DEFAULT true,

  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),

  UNIQUE(owner_user_id, name)
);

DROP TRIGGER IF EXISTS trg_workout_template_updated_at ON lifeswitch_training.workout_template;
CREATE TRIGGER trg_workout_template_updated_at
BEFORE UPDATE ON lifeswitch_training.workout_template
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_workout_template_owner ON lifeswitch_training.workout_template(owner_user_id);

CREATE TABLE IF NOT EXISTS lifeswitch_training.workout_template_exercise (
  workout_template_exercise_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workout_template_id          uuid NOT NULL REFERENCES lifeswitch_training.workout_template(workout_template_id) ON DELETE CASCADE,

  -- references my_exercise.exercise_id, not FK for now (catalog ids are text + can change)
  exercise_id                  text NOT NULL,

  sort_order                   int NOT NULL DEFAULT 10,

  planned_sets                 int NOT NULL DEFAULT 3,
  default_weight               numeric NOT NULL DEFAULT 0,
  default_reps                 int NOT NULL DEFAULT 10,
  flags                        text,

  created_at                   timestamptz NOT NULL DEFAULT now(),
  updated_at                   timestamptz NOT NULL DEFAULT now(),

  UNIQUE(workout_template_id, exercise_id)
);

DROP TRIGGER IF EXISTS trg_wkt_ex_updated_at ON lifeswitch_training.workout_template_exercise;
CREATE TRIGGER trg_wkt_ex_updated_at
BEFORE UPDATE ON lifeswitch_training.workout_template_exercise
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_wkt_ex_template ON lifeswitch_training.workout_template_exercise(workout_template_id);
CREATE INDEX IF NOT EXISTS ix_wkt_ex_sort ON lifeswitch_training.workout_template_exercise(workout_template_id, sort_order);

COMMIT;
