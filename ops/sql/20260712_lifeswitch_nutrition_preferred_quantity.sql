BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- Keep per-100g nutrients canonical while recording their current source.
ALTER TABLE lifeswitch_nutrition.my_food
  ADD COLUMN IF NOT EXISTS source_display_name text,
  ADD COLUMN IF NOT EXISTS nutrient_source text,
  ADD COLUMN IF NOT EXISTS nutrient_source_detail text,
  ADD COLUMN IF NOT EXISTS nutrient_updated_at timestamptz,
  ADD COLUMN IF NOT EXISTS preferred_mode text,
  ADD COLUMN IF NOT EXISTS preferred_quantity numeric(12,3),
  ADD COLUMN IF NOT EXISTS preferred_serving_id uuid;

UPDATE lifeswitch_nutrition.my_food
SET
  source_display_name = COALESCE(NULLIF(btrim(source_display_name), ''), display_name),
  nutrient_source = COALESCE(
    NULLIF(btrim(nutrient_source), ''),
    CASE
      WHEN source = 'usda_fdc' OR source_type = 'usda' THEN 'usda'
      WHEN source_type = 'catalog' THEN 'catalog'
      ELSE 'manual'
    END
  ),
  nutrient_updated_at = COALESCE(nutrient_updated_at, updated_at, created_at, now());

-- Preserve current behavior during migration: saved default grams win, then
-- the default serving, then a safe 100-gram fallback for later review.
WITH preference_seed AS (
  SELECT
    f.my_food_id,
    o.default_grams,
    s.my_food_serving_id
  FROM lifeswitch_nutrition.my_food f
  LEFT JOIN lifeswitch_nutrition.my_food_override o
    ON o.owner_user_id = f.owner_user_id
   AND o.my_food_id = f.my_food_id
  LEFT JOIN lifeswitch_nutrition.my_food_serving s
    ON s.my_food_id = f.my_food_id
   AND s.is_default = true
)
UPDATE lifeswitch_nutrition.my_food f
SET
  preferred_mode = CASE
    WHEN seed.default_grams > 0 THEN 'grams'
    WHEN seed.my_food_serving_id IS NOT NULL THEN 'serving'
    ELSE 'grams'
  END,
  preferred_quantity = CASE
    WHEN seed.default_grams > 0 THEN seed.default_grams
    WHEN seed.my_food_serving_id IS NOT NULL THEN 1
    ELSE 100
  END,
  preferred_serving_id = CASE
    WHEN seed.default_grams > 0 THEN NULL
    ELSE seed.my_food_serving_id
  END
FROM preference_seed seed
WHERE seed.my_food_id = f.my_food_id
  AND (f.preferred_mode IS NULL OR f.preferred_quantity IS NULL);

ALTER TABLE lifeswitch_nutrition.my_food
  ALTER COLUMN nutrient_source SET DEFAULT 'manual',
  ALTER COLUMN nutrient_source SET NOT NULL,
  ALTER COLUMN nutrient_updated_at SET DEFAULT now(),
  ALTER COLUMN nutrient_updated_at SET NOT NULL,
  ALTER COLUMN preferred_mode SET DEFAULT 'grams',
  ALTER COLUMN preferred_mode SET NOT NULL,
  ALTER COLUMN preferred_quantity SET DEFAULT 100,
  ALTER COLUMN preferred_quantity SET NOT NULL;

-- name is the selectable unit; grams is grams per unit. source_label keeps
-- original imported wording when cleanup later normalizes labels.
ALTER TABLE lifeswitch_nutrition.my_food_serving
  ADD COLUMN IF NOT EXISTS source_type text,
  ADD COLUMN IF NOT EXISTS source_label text,
  ADD COLUMN IF NOT EXISTS is_active boolean;

UPDATE lifeswitch_nutrition.my_food_serving
SET
  source_type = COALESCE(NULLIF(btrim(source_type), ''), 'legacy'),
  source_label = COALESCE(NULLIF(btrim(source_label), ''), name),
  is_active = COALESCE(is_active, true);

ALTER TABLE lifeswitch_nutrition.my_food_serving
  ALTER COLUMN source_type SET DEFAULT 'manual',
  ALTER COLUMN source_type SET NOT NULL,
  ALTER COLUMN is_active SET DEFAULT true,
  ALTER COLUMN is_active SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'lifeswitch_nutrition.my_food'::regclass
      AND conname = 'ck_my_food_nutrient_source_nonempty'
  ) THEN
    ALTER TABLE lifeswitch_nutrition.my_food
      ADD CONSTRAINT ck_my_food_nutrient_source_nonempty
      CHECK (btrim(nutrient_source) <> '');
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'lifeswitch_nutrition.my_food'::regclass
      AND conname = 'ck_my_food_preferred_mode'
  ) THEN
    ALTER TABLE lifeswitch_nutrition.my_food
      ADD CONSTRAINT ck_my_food_preferred_mode
      CHECK (preferred_mode IN ('grams', 'serving'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'lifeswitch_nutrition.my_food'::regclass
      AND conname = 'ck_my_food_preferred_quantity_positive'
  ) THEN
    ALTER TABLE lifeswitch_nutrition.my_food
      ADD CONSTRAINT ck_my_food_preferred_quantity_positive
      CHECK (preferred_quantity > 0);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'lifeswitch_nutrition.my_food'::regclass
      AND conname = 'ck_my_food_preferred_serving_pair'
  ) THEN
    ALTER TABLE lifeswitch_nutrition.my_food
      ADD CONSTRAINT ck_my_food_preferred_serving_pair
      CHECK (
        (preferred_mode = 'grams' AND preferred_serving_id IS NULL)
        OR
        (preferred_mode = 'serving' AND preferred_serving_id IS NOT NULL)
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'lifeswitch_nutrition.my_food'::regclass
      AND conname = 'my_food_preferred_serving_food_fkey'
  ) THEN
    ALTER TABLE lifeswitch_nutrition.my_food
      ADD CONSTRAINT my_food_preferred_serving_food_fkey
      FOREIGN KEY (preferred_serving_id, my_food_id)
      REFERENCES lifeswitch_nutrition.my_food_serving
        (my_food_serving_id, my_food_id)
      ON DELETE RESTRICT;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'lifeswitch_nutrition.my_food_serving'::regclass
      AND conname = 'ck_my_food_serving_grams_positive'
  ) THEN
    ALTER TABLE lifeswitch_nutrition.my_food_serving
      ADD CONSTRAINT ck_my_food_serving_grams_positive
      CHECK (grams > 0);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'lifeswitch_nutrition.my_food_serving'::regclass
      AND conname = 'ck_my_food_serving_source_type_nonempty'
  ) THEN
    ALTER TABLE lifeswitch_nutrition.my_food_serving
      ADD CONSTRAINT ck_my_food_serving_source_type_nonempty
      CHECK (btrim(source_type) <> '');
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_my_food_preferred_serving
  ON lifeswitch_nutrition.my_food(preferred_serving_id)
  WHERE preferred_serving_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_my_food_serving_active_food
  ON lifeswitch_nutrition.my_food_serving(my_food_id)
  WHERE is_active = true;

COMMIT;
