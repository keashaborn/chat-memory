BEGIN;

-- 1) Add serving-based fields to meal_item (templates)
ALTER TABLE lifeswitch_nutrition.meal_item
  ADD COLUMN IF NOT EXISTS my_food_serving_id uuid,
  ADD COLUMN IF NOT EXISTS qty_servings numeric(12,3);

-- 2) Foreign key to serving presets
ALTER TABLE lifeswitch_nutrition.meal_item
  DROP CONSTRAINT IF EXISTS meal_item_my_food_serving_fk;

ALTER TABLE lifeswitch_nutrition.meal_item
  ADD CONSTRAINT meal_item_my_food_serving_fk
  FOREIGN KEY (my_food_serving_id)
  REFERENCES lifeswitch_nutrition.my_food_serving(my_food_serving_id)
  ON DELETE RESTRICT;

-- 3) Exactly one quantity mode:
--    grams mode: qty_g IS NOT NULL and serving fields NULL
--    serving mode: qty_g IS NULL and both serving fields present
ALTER TABLE lifeswitch_nutrition.meal_item
  DROP CONSTRAINT IF EXISTS ck_meal_item_one_qty_mode;

ALTER TABLE lifeswitch_nutrition.meal_item
  ADD CONSTRAINT ck_meal_item_one_qty_mode
  CHECK (
    (qty_g IS NOT NULL AND my_food_serving_id IS NULL AND qty_servings IS NULL)
    OR
    (qty_g IS NULL AND my_food_serving_id IS NOT NULL AND qty_servings IS NOT NULL AND qty_servings > 0)
  );

-- 4) Index for joins/lookups
CREATE INDEX IF NOT EXISTS ix_meal_item_serving
  ON lifeswitch_nutrition.meal_item(my_food_serving_id);

COMMIT;
