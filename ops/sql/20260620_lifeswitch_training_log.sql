BEGIN;

CREATE TABLE IF NOT EXISTS lifeswitch_training.training_session (
  training_session_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id       uuid NOT NULL,

  day                 date NOT NULL,
  workout_template_id uuid,
  name                text NOT NULL,
  notes               text,

  started_at          timestamptz,
  finished_at         timestamptz,
  is_active           boolean NOT NULL DEFAULT true,

  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_training_session_updated_at ON lifeswitch_training.training_session;
CREATE TRIGGER trg_training_session_updated_at
BEFORE UPDATE ON lifeswitch_training.training_session
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_training_session_owner_day
  ON lifeswitch_training.training_session(owner_user_id, day DESC);

CREATE INDEX IF NOT EXISTS ix_training_session_template
  ON lifeswitch_training.training_session(workout_template_id);


CREATE TABLE IF NOT EXISTS lifeswitch_training.training_set_log (
  training_set_log_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  training_session_id uuid NOT NULL REFERENCES lifeswitch_training.training_session(training_session_id) ON DELETE CASCADE,
  owner_user_id       uuid NOT NULL,

  workout_template_id uuid,
  exercise_id         text NOT NULL,
  exercise_name       text NOT NULL,

  exercise_sort_order int NOT NULL DEFAULT 0,
  set_index           int NOT NULL DEFAULT 1,

  weight              numeric NOT NULL DEFAULT 0,
  reps                int NOT NULL DEFAULT 0,
  volume              numeric NOT NULL DEFAULT 0,

  flags               text,
  notes               text,

  is_active           boolean NOT NULL DEFAULT true,

  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_training_set_log_updated_at ON lifeswitch_training.training_set_log;
CREATE TRIGGER trg_training_set_log_updated_at
BEFORE UPDATE ON lifeswitch_training.training_set_log
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_training_set_log_owner
  ON lifeswitch_training.training_set_log(owner_user_id);

CREATE INDEX IF NOT EXISTS ix_training_set_log_session
  ON lifeswitch_training.training_set_log(training_session_id, exercise_sort_order, set_index);

CREATE INDEX IF NOT EXISTS ix_training_set_log_exercise
  ON lifeswitch_training.training_set_log(owner_user_id, exercise_id);

COMMIT;
