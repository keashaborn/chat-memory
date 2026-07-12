BEGIN;

CREATE TEMP TABLE phase3_verified_serving (
  source_id text PRIMARY KEY,
  name text NOT NULL,
  grams numeric(12,3) NOT NULL CHECK (grams > 0)
) ON COMMIT DROP;

-- Values verified against FoodData Central label nutrients on 2026-07-12.
INSERT INTO phase3_verified_serving (source_id, name, grams) VALUES
  ('1458977', '1 Bottle (591 mL)', 600.000),
  ('2088226', '5 PIECES', 40.000),
  ('2103943', '1 EGG', 50.000),
  ('2107271', '2 SLICES', 50.000),
  ('2161414', '1 tbsp', 14.000),
  ('2501671', '1 Bottle', 414.000),
  ('2742751', '1 Bottle', 414.000);

-- Refresh an existing canonical serving without overwriting manual provenance.
UPDATE lifeswitch_nutrition.my_food_serving s
SET grams = v.grams,
    source_type = CASE WHEN s.source_type IN ('legacy', 'usda') THEN 'usda' ELSE s.source_type END,
    source_label = CASE WHEN s.source_type IN ('legacy', 'usda') THEN v.name ELSE s.source_label END,
    is_active = true,
    updated_at = now()
FROM lifeswitch_nutrition.my_food f,
     phase3_verified_serving v
WHERE s.my_food_id = f.my_food_id
  AND f.source_type = 'usda'
  AND f.source_id = v.source_id
  AND lower(s.name) = lower(v.name);

-- Create the canonical serving where the legacy importer skipped it.
INSERT INTO lifeswitch_nutrition.my_food_serving (
  my_food_id,
  name,
  grams,
  is_default,
  source_type,
  source_label,
  is_active
)
SELECT
  f.my_food_id,
  v.name,
  v.grams,
  false,
  'usda',
  v.name,
  true
FROM lifeswitch_nutrition.my_food f
JOIN phase3_verified_serving v
  ON v.source_id = f.source_id
WHERE f.source_type = 'usda'
  AND f.is_active = true
  AND NOT EXISTS (
    SELECT 1
    FROM lifeswitch_nutrition.my_food_serving s
    WHERE s.my_food_id = f.my_food_id
      AND lower(s.name) = lower(v.name)
  );

-- Retire only known incorrect legacy aliases; retain unrelated manual servings.
UPDATE lifeswitch_nutrition.my_food_serving s
SET is_active = false,
    is_default = false,
    updated_at = now()
FROM lifeswitch_nutrition.my_food f
WHERE s.my_food_id = f.my_food_id
  AND f.source_type = 'usda'
  AND s.source_type = 'legacy'
  AND (
    (f.source_id = '1458977' AND lower(s.name) = 'bottle')
    OR (f.source_id = '2501671' AND lower(s.name) = 'bottle')
    OR (f.source_id = '2161414' AND lower(s.name) IN ('tble', 'tbs'))
    OR (f.source_id = '2103943' AND lower(s.name) IN ('egg', 'one egg', 'servings'))
  );

-- Make the verified serving the only default for these source records.
UPDATE lifeswitch_nutrition.my_food_serving s
SET is_default = false,
    updated_at = now()
FROM lifeswitch_nutrition.my_food f
JOIN phase3_verified_serving v
  ON v.source_id = f.source_id
WHERE s.my_food_id = f.my_food_id
  AND f.source_type = 'usda'
  AND s.is_default = true
  AND lower(s.name) <> lower(v.name);

UPDATE lifeswitch_nutrition.my_food_serving s
SET is_default = true,
    is_active = true,
    updated_at = now()
FROM lifeswitch_nutrition.my_food f
JOIN phase3_verified_serving v
  ON v.source_id = f.source_id
WHERE s.my_food_id = f.my_food_id
  AND f.source_type = 'usda'
  AND lower(s.name) = lower(v.name);

UPDATE lifeswitch_nutrition.my_food f
SET preferred_mode = 'serving',
    preferred_quantity = 1,
    preferred_serving_id = s.my_food_serving_id,
    updated_at = now()
FROM phase3_verified_serving v
JOIN lifeswitch_nutrition.my_food_serving s
  ON lower(s.name) = lower(v.name)
WHERE f.source_type = 'usda'
  AND f.source_id = v.source_id
  AND s.my_food_id = f.my_food_id
  AND s.is_active = true;

-- If one active serving exactly preserves the user's gram default, prefer its
-- natural label without changing the calculated quantity.
WITH single_active_serving AS (
  SELECT
    f.my_food_id,
    (array_agg(s.my_food_serving_id))[1] AS my_food_serving_id
  FROM lifeswitch_nutrition.my_food f
  JOIN lifeswitch_nutrition.my_food_serving s
    ON s.my_food_id = f.my_food_id
   AND s.is_active = true
  WHERE f.is_active = true
    AND f.preferred_mode = 'grams'
  GROUP BY f.my_food_id, f.preferred_quantity
  HAVING count(*) = 1
     AND abs(max(s.grams) - f.preferred_quantity) <= 0.500
)
UPDATE lifeswitch_nutrition.my_food f
SET preferred_mode = 'serving',
    preferred_quantity = 1,
    preferred_serving_id = candidate.my_food_serving_id,
    updated_at = now()
FROM single_active_serving candidate
WHERE f.my_food_id = candidate.my_food_id;

-- USDA omits total fat for this label. Preserve the record but mark the
-- 4/4/9-derived value as an explicit label inference.
UPDATE lifeswitch_nutrition.my_food
SET fat_g = round(((kcal - protein_g * 4 - carbs_g * 4) / 9)::numeric, 3),
    nutrient_source = 'label',
    nutrient_source_detail = 'USDA label; total fat inferred from calories with 4/4/9 because USDA omits total fat',
    nutrient_updated_at = now(),
    is_verified = false,
    updated_at = now()
WHERE source_type = 'usda'
  AND source_id = '2411713'
  AND fat_g IS NULL
  AND kcal > protein_g * 4 + carbs_g * 4;

-- USDA omits energy for this complete macro label. Store the transparent
-- 4/4/9 estimate and leave the record unverified.
UPDATE lifeswitch_nutrition.my_food
SET kcal = round((protein_g * 4 + carbs_g * 4 + fat_g * 9)::numeric, 3),
    nutrient_source = 'label',
    nutrient_source_detail = 'USDA label; calories inferred from macros with 4/4/9 because USDA omits energy',
    nutrient_updated_at = now(),
    is_verified = false,
    updated_at = now()
WHERE source_type = 'usda'
  AND source_id = '2256654'
  AND kcal IS NULL
  AND protein_g IS NOT NULL
  AND carbs_g IS NOT NULL
  AND fat_g IS NOT NULL;

-- This USDA seasoning record reports 625 kcal/100g with zero macros and no
-- label nutrient data. It cannot be repaired deterministically.
UPDATE lifeswitch_nutrition.my_food
SET is_active = false,
    updated_at = now()
WHERE source_type = 'usda'
  AND source_id = '2375358'
  AND is_active = true
  AND coalesce(kcal, 0) > 0
  AND coalesce(protein_g, 0) = 0
  AND coalesce(carbs_g, 0) = 0
  AND coalesce(fat_g, 0) = 0;

COMMIT;
