BEGIN;

ALTER TABLE lifeswitch_nutrition.nutrition_day
  ADD COLUMN IF NOT EXISTS completed_at timestamptz;

CREATE TABLE IF NOT EXISTS lifeswitch_nutrition.nutrition_day_completion_event (
  nutrition_day_completion_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nutrition_day_id uuid NOT NULL
    REFERENCES lifeswitch_nutrition.nutrition_day(nutrition_day_id) ON DELETE RESTRICT,
  owner_user_id uuid NOT NULL,
  actor_user_id uuid,
  action text NOT NULL CHECK (
    action IN ('completed', 'reopened', 'reopened_after_entry_change')
  ),
  source text NOT NULL CHECK (source IN ('user', 'system')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_nutrition_day_completion_event_day_created
  ON lifeswitch_nutrition.nutrition_day_completion_event
  (nutrition_day_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_nutrition_day_completion_event_owner_created
  ON lifeswitch_nutrition.nutrition_day_completion_event
  (owner_user_id, created_at DESC);

CREATE OR REPLACE FUNCTION lifeswitch_nutrition.tg_reopen_completed_nutrition_day()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  affected_day_id uuid;
  affected_owner_id uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    affected_day_id := OLD.nutrition_day_id;
  ELSE
    affected_day_id := NEW.nutrition_day_id;
  END IF;

  UPDATE lifeswitch_nutrition.nutrition_day
  SET completed_at = NULL
  WHERE nutrition_day_id = affected_day_id
    AND completed_at IS NOT NULL
  RETURNING owner_user_id INTO affected_owner_id;

  IF affected_owner_id IS NOT NULL THEN
    INSERT INTO lifeswitch_nutrition.nutrition_day_completion_event
      (nutrition_day_id, owner_user_id, actor_user_id, action, source)
    VALUES
      (affected_day_id, affected_owner_id, NULL, 'reopened_after_entry_change', 'system');
  END IF;

  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_nutrition_entry_reopen_completed_day
  ON lifeswitch_nutrition.nutrition_entry;
CREATE TRIGGER trg_nutrition_entry_reopen_completed_day
AFTER INSERT OR UPDATE OR DELETE ON lifeswitch_nutrition.nutrition_entry
FOR EACH ROW EXECUTE FUNCTION lifeswitch_nutrition.tg_reopen_completed_nutrition_day();

CREATE OR REPLACE FUNCTION lifeswitch_nutrition.tg_reject_completion_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'nutrition day completion events are append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_nutrition_day_completion_event_append_only
  ON lifeswitch_nutrition.nutrition_day_completion_event;
CREATE TRIGGER trg_nutrition_day_completion_event_append_only
BEFORE UPDATE OR DELETE ON lifeswitch_nutrition.nutrition_day_completion_event
FOR EACH ROW EXECUTE FUNCTION lifeswitch_nutrition.tg_reject_completion_event_mutation();

COMMIT;
