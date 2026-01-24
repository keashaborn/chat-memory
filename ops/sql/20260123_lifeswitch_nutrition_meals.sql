BEGIN;

CREATE TABLE IF NOT EXISTS lifeswitch_nutrition.meal (
  meal_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  name text NOT NULL,
  meal_type text NOT NULL DEFAULT 'other', -- breakfast|lunch|dinner|snack|other
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_user_id, name)
);

DROP TRIGGER IF EXISTS trg_meal_updated_at ON lifeswitch_nutrition.meal;
CREATE TRIGGER trg_meal_updated_at
BEFORE UPDATE ON lifeswitch_nutrition.meal
FOR EACH ROW EXECUTE FUNCTION lifeswitch_nutrition.tg_set_updated_at();

CREATE TABLE IF NOT EXISTS lifeswitch_nutrition.meal_item (
  meal_item_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  meal_id uuid NOT NULL REFERENCES lifeswitch_nutrition.meal(meal_id) ON DELETE CASCADE,
  my_food_id uuid NOT NULL REFERENCES lifeswitch_nutrition.my_food(my_food_id) ON DELETE RESTRICT,
  sort_order int NOT NULL DEFAULT 0,
  qty_g numeric(12,3),
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_meal_item_updated_at ON lifeswitch_nutrition.meal_item;
CREATE TRIGGER trg_meal_item_updated_at
BEFORE UPDATE ON lifeswitch_nutrition.meal_item
FOR EACH ROW EXECUTE FUNCTION lifeswitch_nutrition.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_meal_owner ON lifeswitch_nutrition.meal(owner_user_id);
CREATE INDEX IF NOT EXISTS ix_meal_item_meal ON lifeswitch_nutrition.meal_item(meal_id);
CREATE INDEX IF NOT EXISTS ix_meal_item_my_food ON lifeswitch_nutrition.meal_item(my_food_id);

COMMIT;
