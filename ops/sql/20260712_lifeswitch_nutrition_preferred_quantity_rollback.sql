BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DROP INDEX IF EXISTS lifeswitch_nutrition.ix_my_food_serving_active_food;
DROP INDEX IF EXISTS lifeswitch_nutrition.ix_my_food_preferred_serving;

ALTER TABLE lifeswitch_nutrition.my_food
  DROP CONSTRAINT IF EXISTS my_food_preferred_serving_food_fkey,
  DROP CONSTRAINT IF EXISTS ck_my_food_preferred_serving_pair,
  DROP CONSTRAINT IF EXISTS ck_my_food_preferred_quantity_positive,
  DROP CONSTRAINT IF EXISTS ck_my_food_preferred_mode,
  DROP CONSTRAINT IF EXISTS ck_my_food_nutrient_source_nonempty;

ALTER TABLE lifeswitch_nutrition.my_food_serving
  DROP CONSTRAINT IF EXISTS ck_my_food_serving_source_type_nonempty,
  DROP CONSTRAINT IF EXISTS ck_my_food_serving_grams_positive;

ALTER TABLE lifeswitch_nutrition.my_food
  DROP COLUMN IF EXISTS preferred_serving_id,
  DROP COLUMN IF EXISTS preferred_quantity,
  DROP COLUMN IF EXISTS preferred_mode,
  DROP COLUMN IF EXISTS nutrient_updated_at,
  DROP COLUMN IF EXISTS nutrient_source_detail,
  DROP COLUMN IF EXISTS nutrient_source,
  DROP COLUMN IF EXISTS source_display_name;

ALTER TABLE lifeswitch_nutrition.my_food_serving
  DROP COLUMN IF EXISTS is_active,
  DROP COLUMN IF EXISTS source_label,
  DROP COLUMN IF EXISTS source_type;

COMMIT;
