BEGIN;

ALTER TABLE lifeswitch_nutrition.my_food_serving
  DROP CONSTRAINT IF EXISTS uq_my_food_serving_id_food,
  ADD CONSTRAINT uq_my_food_serving_id_food
    UNIQUE (my_food_serving_id, my_food_id);

ALTER TABLE lifeswitch_nutrition.nutrition_entry
  ADD COLUMN IF NOT EXISTS my_food_serving_id uuid,
  ADD COLUMN IF NOT EXISTS qty_servings numeric(12,3);

ALTER TABLE lifeswitch_nutrition.nutrition_entry
  DROP CONSTRAINT IF EXISTS nutrition_entry_serving_food_fkey,
  ADD CONSTRAINT nutrition_entry_serving_food_fkey
    FOREIGN KEY (my_food_serving_id, my_food_id)
    REFERENCES lifeswitch_nutrition.my_food_serving
      (my_food_serving_id, my_food_id)
    ON DELETE RESTRICT;

ALTER TABLE lifeswitch_nutrition.nutrition_entry
  DROP CONSTRAINT IF EXISTS ck_nutrition_entry_serving_pair,
  ADD CONSTRAINT ck_nutrition_entry_serving_pair CHECK (
    (
      my_food_serving_id IS NULL
      AND qty_servings IS NULL
    )
    OR
    (
      my_food_id IS NOT NULL
      AND my_food_serving_id IS NOT NULL
      AND qty_servings IS NOT NULL
      AND qty_servings > 0
    )
  );

ALTER TABLE lifeswitch_nutrition.nutrition_entry
  DROP CONSTRAINT IF EXISTS ck_nutrition_entry_food_qty_g,
  ADD CONSTRAINT ck_nutrition_entry_food_qty_g CHECK (
    my_food_id IS NULL
    OR (qty_g IS NOT NULL AND qty_g > 0)
  );

CREATE INDEX IF NOT EXISTS ix_nutrition_entry_my_food_serving
  ON lifeswitch_nutrition.nutrition_entry(my_food_serving_id);

COMMIT;
