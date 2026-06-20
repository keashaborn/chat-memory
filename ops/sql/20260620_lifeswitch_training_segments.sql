BEGIN;

ALTER TABLE lifeswitch_training.workout_template_exercise
  ADD COLUMN IF NOT EXISTS set_type text NOT NULL DEFAULT 'straight';

ALTER TABLE lifeswitch_training.training_set_log
  ADD COLUMN IF NOT EXISTS set_type text NOT NULL DEFAULT 'straight';

CREATE TABLE IF NOT EXISTS lifeswitch_training.workout_template_exercise_segment (
  workout_template_exercise_segment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workout_template_exercise_id         uuid NOT NULL REFERENCES lifeswitch_training.workout_template_exercise(workout_template_exercise_id) ON DELETE CASCADE,

  segment_index                        int NOT NULL DEFAULT 1,
  label                                text,

  default_weight                       numeric NOT NULL DEFAULT 0,
  default_reps                         int NOT NULL DEFAULT 0,

  created_at                           timestamptz NOT NULL DEFAULT now(),
  updated_at                           timestamptz NOT NULL DEFAULT now(),

  UNIQUE(workout_template_exercise_id, segment_index)
);

DROP TRIGGER IF EXISTS trg_wkt_ex_seg_updated_at ON lifeswitch_training.workout_template_exercise_segment;
CREATE TRIGGER trg_wkt_ex_seg_updated_at
BEFORE UPDATE ON lifeswitch_training.workout_template_exercise_segment
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_wkt_ex_seg_parent
  ON lifeswitch_training.workout_template_exercise_segment(workout_template_exercise_id, segment_index);

CREATE TABLE IF NOT EXISTS lifeswitch_training.training_set_log_segment (
  training_set_log_segment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  training_set_log_id         uuid NOT NULL REFERENCES lifeswitch_training.training_set_log(training_set_log_id) ON DELETE CASCADE,

  segment_index               int NOT NULL DEFAULT 1,
  label                       text,

  weight                      numeric NOT NULL DEFAULT 0,
  reps                        int NOT NULL DEFAULT 0,
  volume                      numeric NOT NULL DEFAULT 0,

  notes                       text,

  created_at                  timestamptz NOT NULL DEFAULT now(),
  updated_at                  timestamptz NOT NULL DEFAULT now(),

  UNIQUE(training_set_log_id, segment_index)
);

DROP TRIGGER IF EXISTS trg_training_set_log_segment_updated_at ON lifeswitch_training.training_set_log_segment;
CREATE TRIGGER trg_training_set_log_segment_updated_at
BEFORE UPDATE ON lifeswitch_training.training_set_log_segment
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_training_set_log_segment_parent
  ON lifeswitch_training.training_set_log_segment(training_set_log_id, segment_index);

COMMIT;
