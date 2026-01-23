BEGIN;

CREATE SCHEMA IF NOT EXISTS lifeswitch_nutrition AUTHORIZATION sage;

-- self-contained updated_at trigger function (avoid relying on other schemas)
CREATE OR REPLACE FUNCTION lifeswitch_nutrition.tg_set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

-- Private, user-owned canonical foods (may be copied from catalog or entered manually)
CREATE TABLE IF NOT EXISTS lifeswitch_nutrition.my_food (
  my_food_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,

  display_name text NOT NULL,
  variant text,                 -- e.g. "96/4", "lean", "brand X"

  -- catalog | label | ai
  source_type text NOT NULL DEFAULT 'catalog',
  source_food_id uuid,          -- optional pointer to catalog food_id
  source text,                  -- e.g. usda_fdc, openfoodfacts, label, ai
  source_id text,               -- e.g. fdc_id, upc, etc
  barcode text,

  -- store per-100g macros (decouples from catalog changes)
  basis text NOT NULL DEFAULT 'per_100g',
  kcal numeric(12,3),
  protein_g numeric(12,3),
  carbs_g numeric(12,3),
  fat_g numeric(12,3),
  fiber_g numeric(12,3),
  sugar_g numeric(12,3),
  sodium_mg numeric(12,3),

  is_verified boolean NOT NULL DEFAULT false,
  is_active boolean NOT NULL DEFAULT true,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_my_food_owner ON lifeswitch_nutrition.my_food(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_my_food_owner_name ON lifeswitch_nutrition.my_food(owner_user_id, lower(display_name));

DROP TRIGGER IF EXISTS trg_my_food_updated_at ON lifeswitch_nutrition.my_food;
CREATE TRIGGER trg_my_food_updated_at
BEFORE UPDATE ON lifeswitch_nutrition.my_food
FOR EACH ROW EXECUTE FUNCTION lifeswitch_nutrition.tg_set_updated_at();

-- User-defined serving units (grams as canonical)
CREATE TABLE IF NOT EXISTS lifeswitch_nutrition.my_food_serving (
  my_food_serving_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  my_food_id uuid NOT NULL REFERENCES lifeswitch_nutrition.my_food(my_food_id) ON DELETE CASCADE,

  name text NOT NULL,            -- e.g. "1 patty", "1/3 lb", "slice"
  grams numeric(12,3) NOT NULL,
  is_default boolean NOT NULL DEFAULT false,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_my_food_serving_food ON lifeswitch_nutrition.my_food_serving(my_food_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_my_food_serving_default
  ON lifeswitch_nutrition.my_food_serving(my_food_id)
  WHERE is_default;

DROP TRIGGER IF EXISTS trg_my_food_serving_updated_at ON lifeswitch_nutrition.my_food_serving;
CREATE TRIGGER trg_my_food_serving_updated_at
BEFORE UPDATE ON lifeswitch_nutrition.my_food_serving
FOR EACH ROW EXECUTE FUNCTION lifeswitch_nutrition.tg_set_updated_at();

COMMIT;
