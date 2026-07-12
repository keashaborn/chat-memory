BEGIN;

DO $cleanup$
DECLARE
  target_owner constant uuid := '1240822d-ac9a-4096-95aa-e2b24d36ef50';

  cheddar_catalog constant uuid := '5512c4d5-b986-44b3-83c6-4d4ce0932a31';
  egg_catalog constant uuid := '773936ff-4f1b-4e79-a777-292c6162d578';
  chicken_catalog constant uuid := '2e3142c3-bffc-431b-b67d-50bf309ccd3d';

  cheddar_my_food constant uuid := 'd8483066-ae71-4fab-84fa-de9eb4e89433';
  egg_my_food constant uuid := 'c80b41de-8b00-4db4-8edf-5030a4d34ef3';
  chicken_my_food constant uuid := '8290b594-ba11-4e64-9b93-29fb587a8cf2';
  egg_serving constant uuid := '3153d9ea-6b7f-4d41-854c-d5df699883eb';

  cheddar_serving uuid;
  legacy_count integer;
  target_count integer;
BEGIN
  SELECT count(*)
  INTO legacy_count
  FROM lifeswitch_nutrition.meal_plan_item
  WHERE food_id IS NOT NULL;

  IF legacy_count = 0 THEN
    RAISE NOTICE 'No legacy Meal Plan foods remain; cleanup already applied.';
    RETURN;
  END IF;

  IF legacy_count <> 5 THEN
    RAISE EXCEPTION
      'Expected exactly 5 legacy Meal Plan items, found %; refusing cleanup',
      legacy_count;
  END IF;

  SELECT count(*)
  INTO target_count
  FROM lifeswitch_nutrition.meal_plan_item i
  JOIN lifeswitch_nutrition.meal_plan p
    ON p.meal_plan_id = i.meal_plan_id
  WHERE p.owner_user_id = target_owner
    AND (
      (i.food_id = cheddar_catalog AND i.qty_g IN (30, 50, 100))
      OR (i.food_id = egg_catalog AND i.qty_g = 150)
      OR (i.food_id = chicken_catalog AND i.qty_g = 200)
    );

  IF target_count <> 5 THEN
    RAISE EXCEPTION
      'Expected all 5 legacy items to match the reviewed owner/food/quantity set, matched %',
      target_count;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.my_food
    WHERE my_food_id = egg_my_food
      AND owner_user_id = target_owner
      AND is_active
  ) THEN
    RAISE EXCEPTION 'Reviewed active egg library food is missing';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.my_food_serving
    WHERE my_food_serving_id = egg_serving
      AND my_food_id = egg_my_food
      AND grams = 50
      AND is_active
  ) THEN
    RAISE EXCEPTION 'Reviewed 1 Egg = 50g serving is missing';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.my_food
    WHERE my_food_id = chicken_my_food
      AND owner_user_id = target_owner
      AND is_active
  ) THEN
    RAISE EXCEPTION 'Reviewed active chicken breast library food is missing';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.my_food
    WHERE my_food_id = cheddar_my_food
      AND owner_user_id = target_owner
      AND source_food_id = cheddar_catalog
  ) THEN
    RAISE EXCEPTION 'Reviewed cheddar library record is missing';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.my_food
    WHERE owner_user_id = target_owner
      AND source_type = 'catalog'
      AND source_id = '2057648'
      AND coalesce(variant, '') = ''
      AND is_active
      AND my_food_id <> cheddar_my_food
  ) THEN
    RAISE EXCEPTION 'Another active cheddar catalog record would conflict';
  END IF;

  UPDATE lifeswitch_nutrition.my_food mf
  SET display_name = cf.display_name,
      source_display_name = cf.display_name,
      brand = cf.brand,
      source_type = 'catalog',
      source_food_id = cf.food_id,
      source = cf.source,
      source_id = cf.source_id,
      barcode = cf.barcode,
      basis = cf.basis,
      kcal = cf.kcal,
      protein_g = cf.protein_g,
      carbs_g = cf.carbs_g,
      fat_g = cf.fat_g,
      fiber_g = cf.fiber_g,
      sugar_g = cf.sugar_g,
      sodium_mg = cf.sodium_mg,
      nutrient_source = 'catalog',
      nutrient_source_detail = 'USDA FoodData Central 2057648',
      nutrient_updated_at = now(),
      is_verified = true,
      is_active = true,
      updated_at = now()
  FROM catalog_dev.food cf
  WHERE mf.my_food_id = cheddar_my_food
    AND cf.food_id = cheddar_catalog;

  UPDATE lifeswitch_nutrition.my_food_serving
  SET is_default = false,
      updated_at = now()
  WHERE my_food_id = cheddar_my_food
    AND is_default;

  INSERT INTO lifeswitch_nutrition.my_food_serving
    (my_food_id, name, grams, is_default, source_type, source_label, is_active)
  VALUES
    (cheddar_my_food, '1 oz', 28, true, 'usda', 'USDA: 1 ONZ', true)
  ON CONFLICT (my_food_id, lower(name))
  DO UPDATE SET
    grams = excluded.grams,
    is_default = true,
    source_type = 'usda',
    source_label = excluded.source_label,
    is_active = true,
    updated_at = now()
  RETURNING my_food_serving_id INTO cheddar_serving;

  UPDATE lifeswitch_nutrition.my_food
  SET preferred_mode = 'serving',
      preferred_quantity = 1,
      preferred_serving_id = cheddar_serving,
      updated_at = now()
  WHERE my_food_id = cheddar_my_food;

  UPDATE lifeswitch_nutrition.meal_plan_item i
  SET my_food_id = cheddar_my_food,
      food_id = NULL,
      updated_at = now()
  FROM lifeswitch_nutrition.meal_plan p
  WHERE p.meal_plan_id = i.meal_plan_id
    AND p.owner_user_id = target_owner
    AND i.food_id = cheddar_catalog;

  UPDATE lifeswitch_nutrition.meal_plan_item i
  SET my_food_id = egg_my_food,
      food_id = NULL,
      qty_g = NULL,
      my_food_serving_id = egg_serving,
      qty_servings = 3,
      updated_at = now()
  FROM lifeswitch_nutrition.meal_plan p
  WHERE p.meal_plan_id = i.meal_plan_id
    AND p.owner_user_id = target_owner
    AND i.food_id = egg_catalog;

  UPDATE lifeswitch_nutrition.meal_plan_item i
  SET my_food_id = chicken_my_food,
      food_id = NULL,
      updated_at = now()
  FROM lifeswitch_nutrition.meal_plan p
  WHERE p.meal_plan_id = i.meal_plan_id
    AND p.owner_user_id = target_owner
    AND i.food_id = chicken_catalog;

  IF EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.meal_plan_item
    WHERE food_id IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'Legacy Meal Plan rows remain after cleanup';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.meal_plan_item
    WHERE my_food_id = egg_my_food
      AND qty_g IS NULL
      AND my_food_serving_id = egg_serving
      AND qty_servings = 3
  ) THEN
    RAISE EXCEPTION 'Egg conversion did not preserve 150 resolved grams';
  END IF;

  RAISE NOTICE
    'Migrated 5 legacy Meal Plan items to library foods; cheddar serving id=%',
    cheddar_serving;
END
$cleanup$;

DO $deduplicate_beef$
DECLARE
  target_owner constant uuid := '1240822d-ac9a-4096-95aa-e2b24d36ef50';
  inactive_beef constant uuid := '7cc2ce21-62a6-4955-8442-5c4a1d4ececc';
  active_beef constant uuid := 'b898b610-d4d1-4da4-a364-ee6f260bd28a';
  active_beef_serving constant uuid := '2e0e4f52-6cc2-4098-a2ee-dcb044dc364b';
  saved_meal_count integer;
  plan_count integer;
BEGIN
  SELECT count(*)
  INTO saved_meal_count
  FROM lifeswitch_nutrition.meal_item
  WHERE my_food_id = inactive_beef;

  SELECT count(*)
  INTO plan_count
  FROM lifeswitch_nutrition.meal_plan_item
  WHERE my_food_id = inactive_beef;

  IF saved_meal_count = 0 AND plan_count = 0 THEN
    RAISE NOTICE 'Inactive ground-beef duplicate has no remaining template references.';
    RETURN;
  END IF;

  IF saved_meal_count <> 2 OR plan_count <> 1 THEN
    RAISE EXCEPTION
      'Expected 2 saved-meal and 1 Meal Plan inactive-beef references, found % and %',
      saved_meal_count,
      plan_count;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.meal_item
    WHERE my_food_id = inactive_beef
      AND (
        qty_g <> 150
        OR my_food_serving_id IS NOT NULL
        OR qty_servings IS NOT NULL
      )
  ) OR EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.meal_plan_item
    WHERE my_food_id = inactive_beef
      AND (
        qty_g <> 150
        OR my_food_serving_id IS NOT NULL
        OR qty_servings IS NOT NULL
      )
  ) THEN
    RAISE EXCEPTION 'Inactive ground-beef references no longer match the reviewed 150g quantities';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.my_food
    WHERE my_food_id = active_beef
      AND owner_user_id = target_owner
      AND source_type = 'usda'
      AND source_id = '2523143'
      AND is_active
  ) THEN
    RAISE EXCEPTION 'Reviewed active Hamburger 96/4 library food is missing';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.my_food_serving
    WHERE my_food_serving_id = active_beef_serving
      AND my_food_id = active_beef
      AND grams = 150
      AND is_active
  ) THEN
    RAISE EXCEPTION 'Reviewed 1 Patty = 150g serving is missing';
  END IF;

  UPDATE lifeswitch_nutrition.meal_item
  SET my_food_id = active_beef,
      qty_g = NULL,
      my_food_serving_id = active_beef_serving,
      qty_servings = 1,
      updated_at = now()
  WHERE my_food_id = inactive_beef;

  UPDATE lifeswitch_nutrition.meal_plan_item
  SET my_food_id = active_beef,
      qty_g = NULL,
      my_food_serving_id = active_beef_serving,
      qty_servings = 1,
      updated_at = now()
  WHERE my_food_id = inactive_beef;

  RAISE NOTICE
    'Migrated 2 saved-meal and 1 Meal Plan ground-beef references to 1 Patty = 150g.';
END
$deduplicate_beef$;

DO $repair_inactive_saved_meal_foods$
DECLARE
  target_owner constant uuid := '1240822d-ac9a-4096-95aa-e2b24d36ef50';
  inactive_rice constant uuid := 'ba439196-9e2e-4577-adc5-a9d445a27ec0';
  active_rice constant uuid := '21f040f8-7f5f-481f-a524-f66a453eb085';
  inactive_shake constant uuid := '340e916b-6a12-4b1e-859f-cad8c7550721';
  active_shake constant uuid := 'd6fce466-0414-4f18-9927-cafe61897458';
  target_count integer;
BEGIN
  SELECT count(*)
  INTO target_count
  FROM lifeswitch_nutrition.meal_item
  WHERE my_food_id IN (inactive_rice, inactive_shake);

  IF target_count = 0 THEN
    RAISE NOTICE 'Saved meals have no remaining reviewed inactive food references.';
    RETURN;
  END IF;

  IF target_count <> 2 THEN
    RAISE EXCEPTION
      'Expected 2 reviewed inactive saved-meal foods, found %',
      target_count;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.meal_item
    WHERE my_food_id = inactive_rice
      AND qty_g = 100
      AND my_food_serving_id IS NULL
      AND qty_servings IS NULL
  ) OR NOT EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.meal_item
    WHERE my_food_id = inactive_shake
      AND qty_g = 150
      AND my_food_serving_id IS NULL
      AND qty_servings IS NULL
  ) THEN
    RAISE EXCEPTION 'Inactive saved-meal quantities no longer match the reviewed rows';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.my_food
    WHERE my_food_id = active_rice
      AND owner_user_id = target_owner
      AND display_name = 'ARBORIO RICE'
      AND kcal = 356
      AND carbs_g = 77.78
      AND is_active
  ) THEN
    RAISE EXCEPTION 'Reviewed corrected active Arborio rice is missing';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.my_food active
    JOIN lifeswitch_nutrition.my_food inactive
      ON inactive.my_food_id = inactive_shake
    WHERE active.my_food_id = active_shake
      AND active.owner_user_id = target_owner
      AND active.is_active
      AND active.kcal = inactive.kcal
      AND active.protein_g = inactive.protein_g
      AND active.carbs_g = inactive.carbs_g
      AND active.fat_g = inactive.fat_g
  ) THEN
    RAISE EXCEPTION 'Reviewed active Core Power nutrition no longer matches the inactive shake';
  END IF;

  UPDATE lifeswitch_nutrition.meal_item
  SET my_food_id = active_rice,
      updated_at = now()
  WHERE my_food_id = inactive_rice;

  UPDATE lifeswitch_nutrition.meal_item
  SET my_food_id = active_shake,
      updated_at = now()
  WHERE my_food_id = inactive_shake;

  RAISE NOTICE
    'Mapped saved-meal Arborio rice and Core Power rows to their active corrected library foods.';
END
$repair_inactive_saved_meal_foods$;

COMMIT;
