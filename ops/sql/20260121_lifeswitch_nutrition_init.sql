BEGIN;

CREATE SCHEMA IF NOT EXISTS lifeswitch_nutrition AUTHORIZATION sage;

-- Meal plan template (user-owned)
CREATE TABLE IF NOT EXISTS lifeswitch_nutrition.meal_plan (
  meal_plan_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  name text NOT NULL,
  goal text NOT NULL DEFAULT 'maintain',   -- cut|bulk|maintain
  target_kcal numeric(12,3),
  target_protein_g numeric(12,3),
  target_carbs_g numeric(12,3),
  target_fat_g numeric(12,3),

  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (owner_user_id, name)
);

DROP TRIGGER IF EXISTS trg_meal_plan_updated_at ON lifeswitch_nutrition.meal_plan;
CREATE TRIGGER trg_meal_plan_updated_at
BEFORE UPDATE ON lifeswitch_nutrition.meal_plan
FOR EACH ROW EXECUTE FUNCTION catalog_dev.tg_set_updated_at();

-- Items inside a meal plan (ordered; grouped by meal label)
CREATE TABLE IF NOT EXISTS lifeswitch_nutrition.meal_plan_item (
  meal_plan_item_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  meal_plan_id uuid NOT NULL REFERENCES lifeswitch_nutrition.meal_plan(meal_plan_id) ON DELETE CASCADE,

  meal_label text NOT NULL,              -- breakfast|lunch|dinner|snack|other
  sort_order int NOT NULL DEFAULT 0,

  food_id uuid NOT NULL REFERENCES catalog_dev.food(food_id),
  qty_g numeric(12,3),                   -- grams (preferred)
  qty_servings numeric(12,3),            -- optional
  notes text,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_meal_plan_item_updated_at ON lifeswitch_nutrition.meal_plan_item;
CREATE TRIGGER trg_meal_plan_item_updated_at
BEFORE UPDATE ON lifeswitch_nutrition.meal_plan_item
FOR EACH ROW EXECUTE FUNCTION catalog_dev.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_meal_plan_owner ON lifeswitch_nutrition.meal_plan(owner_user_id);
CREATE INDEX IF NOT EXISTS ix_meal_plan_item_plan ON lifeswitch_nutrition.meal_plan_item(meal_plan_id);
CREATE INDEX IF NOT EXISTS ix_meal_plan_item_food ON lifeswitch_nutrition.meal_plan_item(food_id);

COMMIT;
