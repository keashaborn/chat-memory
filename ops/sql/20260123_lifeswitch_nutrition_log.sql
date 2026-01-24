BEGIN;

-- One row per day per user (optional but useful for indexing)
CREATE TABLE IF NOT EXISTS lifeswitch_nutrition.nutrition_day (
  nutrition_day_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  day date NOT NULL,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_user_id, day)
);

DROP TRIGGER IF EXISTS trg_nutrition_day_updated_at ON lifeswitch_nutrition.nutrition_day;
CREATE TRIGGER trg_nutrition_day_updated_at
BEFORE UPDATE ON lifeswitch_nutrition.nutrition_day
FOR EACH ROW EXECUTE FUNCTION lifeswitch_nutrition.tg_set_updated_at();

-- Entries: either meal_id OR my_food_id, with grams.
CREATE TABLE IF NOT EXISTS lifeswitch_nutrition.nutrition_entry (
  nutrition_entry_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nutrition_day_id uuid NOT NULL REFERENCES lifeswitch_nutrition.nutrition_day(nutrition_day_id) ON DELETE CASCADE,

  meal_id uuid REFERENCES lifeswitch_nutrition.meal(meal_id) ON DELETE RESTRICT,
  my_food_id uuid REFERENCES lifeswitch_nutrition.my_food(my_food_id) ON DELETE RESTRICT,

  qty_g numeric(12,3),
  sort_order int NOT NULL DEFAULT 0,
  notes text,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_nutrition_entry_one_source CHECK (
    (meal_id is not null and my_food_id is null)
    or
    (meal_id is null and my_food_id is not null)
  )
);

DROP TRIGGER IF EXISTS trg_nutrition_entry_updated_at ON lifeswitch_nutrition.nutrition_entry;
CREATE TRIGGER trg_nutrition_entry_updated_at
BEFORE UPDATE ON lifeswitch_nutrition.nutrition_entry
FOR EACH ROW EXECUTE FUNCTION lifeswitch_nutrition.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_nutrition_day_owner_day ON lifeswitch_nutrition.nutrition_day(owner_user_id, day);
CREATE INDEX IF NOT EXISTS ix_nutrition_entry_day ON lifeswitch_nutrition.nutrition_entry(nutrition_day_id);
CREATE INDEX IF NOT EXISTS ix_nutrition_entry_meal ON lifeswitch_nutrition.nutrition_entry(meal_id);
CREATE INDEX IF NOT EXISTS ix_nutrition_entry_my_food ON lifeswitch_nutrition.nutrition_entry(my_food_id);

COMMIT;
