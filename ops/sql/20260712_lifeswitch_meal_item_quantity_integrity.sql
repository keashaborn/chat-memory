BEGIN;

ALTER TABLE lifeswitch_nutrition.meal_item
  DROP CONSTRAINT IF EXISTS ck_meal_item_one_qty_mode,
  ADD CONSTRAINT ck_meal_item_one_qty_mode CHECK (
    (
      qty_g IS NOT NULL
      AND qty_g > 0
      AND my_food_serving_id IS NULL
      AND qty_servings IS NULL
    )
    OR
    (
      qty_g IS NULL
      AND my_food_serving_id IS NOT NULL
      AND qty_servings IS NOT NULL
      AND qty_servings > 0
    )
  );

ALTER TABLE lifeswitch_nutrition.meal_item
  DROP CONSTRAINT IF EXISTS meal_item_my_food_serving_fk,
  DROP CONSTRAINT IF EXISTS meal_item_serving_food_fkey,
  ADD CONSTRAINT meal_item_serving_food_fkey
    FOREIGN KEY (my_food_serving_id, my_food_id)
    REFERENCES lifeswitch_nutrition.my_food_serving
      (my_food_serving_id, my_food_id)
    ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS ix_meal_item_my_food_serving
  ON lifeswitch_nutrition.meal_item(my_food_serving_id);

COMMIT;
