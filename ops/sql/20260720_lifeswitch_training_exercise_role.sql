BEGIN;

ALTER TABLE lifeswitch_training.my_exercise
  ADD COLUMN IF NOT EXISTS exercise_role text;

UPDATE lifeswitch_training.my_exercise
SET exercise_role = 'strength'
WHERE exercise_role IS NULL;

ALTER TABLE lifeswitch_training.my_exercise
  ALTER COLUMN exercise_role SET DEFAULT 'strength',
  ALTER COLUMN exercise_role SET NOT NULL;

DO $migration$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'my_exercise_role_allowed'
      AND conrelid = 'lifeswitch_training.my_exercise'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.my_exercise
      ADD CONSTRAINT my_exercise_role_allowed
      CHECK (exercise_role IN ('strength', 'rehab'));
  END IF;
END
$migration$;

CREATE INDEX IF NOT EXISTS ix_my_exercise_owner_role_active
  ON lifeswitch_training.my_exercise(owner_user_id, exercise_role)
  WHERE is_active = true;

DO $migration$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'my_exercise_owner_id_unique'
      AND conrelid = 'lifeswitch_training.my_exercise'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.my_exercise
      ADD CONSTRAINT my_exercise_owner_id_unique
      UNIQUE (owner_user_id, my_exercise_id);
  END IF;
END
$migration$;

CREATE TABLE IF NOT EXISTS lifeswitch_training.my_exercise_role_event (
  my_exercise_role_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  my_exercise_id uuid NOT NULL,
  exercise_id text NOT NULL,
  previous_role text,
  new_role text NOT NULL,
  changed_by_user_id uuid NOT NULL,
  change_source text NOT NULL DEFAULT 'user',
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT my_exercise_role_event_roles_allowed CHECK (
    (previous_role IS NULL OR previous_role IN ('strength', 'rehab'))
    AND new_role IN ('strength', 'rehab')
  ),
  CONSTRAINT my_exercise_role_event_owner_exercise_fk
    FOREIGN KEY (owner_user_id, my_exercise_id)
    REFERENCES lifeswitch_training.my_exercise(owner_user_id, my_exercise_id)
    ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_my_exercise_role_event_owner_created
  ON lifeswitch_training.my_exercise_role_event(owner_user_id, created_at DESC);

CREATE OR REPLACE FUNCTION lifeswitch_training.protect_my_exercise_role_event()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
  RAISE EXCEPTION 'my_exercise_role_event is append-only';
END
$function$;

DROP TRIGGER IF EXISTS trg_my_exercise_role_event_append_only
  ON lifeswitch_training.my_exercise_role_event;
CREATE TRIGGER trg_my_exercise_role_event_append_only
BEFORE UPDATE OR DELETE ON lifeswitch_training.my_exercise_role_event
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.protect_my_exercise_role_event();

ALTER TABLE lifeswitch_training.training_set_log
  ADD COLUMN IF NOT EXISTS exercise_role_snapshot text;

DO $migration$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'training_set_log_role_snapshot_allowed'
      AND conrelid = 'lifeswitch_training.training_set_log'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_set_log
      ADD CONSTRAINT training_set_log_role_snapshot_allowed
      CHECK (
        exercise_role_snapshot IS NULL
        OR exercise_role_snapshot IN ('strength', 'rehab')
      );
  END IF;
END
$migration$;

COMMENT ON COLUMN lifeswitch_training.my_exercise.exercise_role IS
  'User-controlled analysis role. Rehab remains logged but is excluded from strength metrics.';

COMMENT ON COLUMN lifeswitch_training.training_set_log.exercise_role_snapshot IS
  'Server-resolved exercise role at capture time. NULL legacy rows follow current library classification.';

COMMIT;
