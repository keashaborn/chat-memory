BEGIN;

-- Search / normalization helpers
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE SCHEMA IF NOT EXISTS catalog_dev AUTHORIZATION sage;

-- Immutable wrapper so we can use unaccent in generated columns + indexes
CREATE OR REPLACE FUNCTION catalog_dev.immutable_unaccent(t text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
  SELECT public.unaccent(t)
$$;

CREATE OR REPLACE FUNCTION catalog_dev.norm_text(t text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
  SELECT lower(catalog_dev.immutable_unaccent(btrim(t)))
$$;

CREATE OR REPLACE FUNCTION catalog_dev.tg_set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END
$$;

-- Brands (optional but useful for machine aliases)
CREATE TABLE IF NOT EXISTS catalog_dev.equipment_brand (
  brand_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  brand_name text NOT NULL,
  brand_name_norm text GENERATED ALWAYS AS (catalog_dev.norm_text(brand_name)) STORED,
  homepage_url text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (brand_name_norm)
);

DROP TRIGGER IF EXISTS trg_equipment_brand_updated_at ON catalog_dev.equipment_brand;
CREATE TRIGGER trg_equipment_brand_updated_at
BEFORE UPDATE ON catalog_dev.equipment_brand
FOR EACH ROW EXECUTE FUNCTION catalog_dev.tg_set_updated_at();

-- Canonical exercises/activities (stable IDs templates import)
CREATE TABLE IF NOT EXISTS catalog_dev.exercise (
  exercise_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug text NOT NULL,
  display_name text NOT NULL,
  display_name_norm text GENERATED ALWAYS AS (catalog_dev.norm_text(display_name)) STORED,

  kind text NOT NULL,
  modality text NOT NULL,
  movement_pattern text,

  primary_muscles text[] NOT NULL DEFAULT '{}',
  secondary_muscles text[] NOT NULL DEFAULT '{}',
  joints text[] NOT NULL DEFAULT '{}',
  equipment_required text[] NOT NULL DEFAULT '{}',

  unilateral boolean NOT NULL DEFAULT false,

  is_public boolean NOT NULL DEFAULT true,
  is_active boolean NOT NULL DEFAULT true,

  -- Defines logging fields (sets/reps/load vs duration/distance/etc.)
  default_log jsonb NOT NULL DEFAULT '{}'::jsonb,

  notes text,
  source text NOT NULL DEFAULT 'seed',

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_exercise_slug_lower CHECK (slug = lower(slug)),
  CONSTRAINT ck_exercise_kind CHECK (kind IN ('strength','cardio','mobility','stretch','skill','other')),
  CONSTRAINT ck_exercise_modality CHECK (modality IN (
    'free_weight','machine_selectorized','machine_plate_loaded','cable','smith','bodyweight','cardio_machine','cardio_outdoor','other'
  )),

  UNIQUE (slug)
);

DROP TRIGGER IF EXISTS trg_exercise_updated_at ON catalog_dev.exercise;
CREATE TRIGGER trg_exercise_updated_at
BEFORE UPDATE ON catalog_dev.exercise
FOR EACH ROW EXECUTE FUNCTION catalog_dev.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_exercise_name_trgm
ON catalog_dev.exercise USING gin (display_name_norm gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ix_exercise_kind_modality
ON catalog_dev.exercise (kind, modality);

-- Alias strings users search (machine names, shorthand, etc.)
CREATE TABLE IF NOT EXISTS catalog_dev.exercise_alias (
  alias_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  exercise_id uuid NOT NULL REFERENCES catalog_dev.exercise(exercise_id) ON DELETE CASCADE,

  alias text NOT NULL,
  alias_norm text GENERATED ALWAYS AS (catalog_dev.norm_text(alias)) STORED,
  locale text NOT NULL DEFAULT 'en',

  brand_id uuid REFERENCES catalog_dev.equipment_brand(brand_id),
  model_name text,

  -- 'user' | 'seed' | 'llm' | 'import'
  source text NOT NULL DEFAULT 'user',

  -- for LLM/heuristic mapping: 0..1
  confidence numeric(4,3),

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (alias_norm, locale)
);

DROP TRIGGER IF EXISTS trg_exercise_alias_updated_at ON catalog_dev.exercise_alias;
CREATE TRIGGER trg_exercise_alias_updated_at
BEFORE UPDATE ON catalog_dev.exercise_alias
FOR EACH ROW EXECUTE FUNCTION catalog_dev.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_exercise_alias_trgm
ON catalog_dev.exercise_alias USING gin (alias_norm gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ix_exercise_alias_exercise
ON catalog_dev.exercise_alias (exercise_id);

-- Foods (macros as columns for fast reads; micros in food_nutrient)
CREATE TABLE IF NOT EXISTS catalog_dev.food (
  food_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  display_name text NOT NULL,
  display_name_norm text GENERATED ALWAYS AS (catalog_dev.norm_text(display_name)) STORED,

  brand text,
  barcode text,

  source text NOT NULL,
  source_id text,
  basis text NOT NULL,

  serving_size_g numeric(12,3),

  kcal numeric(12,3),
  protein_g numeric(12,3),
  carbs_g numeric(12,3),
  fat_g numeric(12,3),
  fiber_g numeric(12,3),
  sugar_g numeric(12,3),
  sodium_mg numeric(12,3),

  is_public boolean NOT NULL DEFAULT true,
  is_active boolean NOT NULL DEFAULT true,

  data jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_food_source CHECK (source IN ('usda_fdc','open_food_facts','user','manual','vendor')),
  CONSTRAINT ck_food_basis CHECK (basis IN ('per_100g','per_serving','per_unit'))
);

DROP TRIGGER IF EXISTS trg_food_updated_at ON catalog_dev.food;
CREATE TRIGGER trg_food_updated_at
BEFORE UPDATE ON catalog_dev.food
FOR EACH ROW EXECUTE FUNCTION catalog_dev.tg_set_updated_at();

CREATE UNIQUE INDEX IF NOT EXISTS uq_food_source_source_id
ON catalog_dev.food (source, source_id)
WHERE source_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_food_barcode
ON catalog_dev.food (barcode)
WHERE barcode IS NOT NULL AND barcode <> '';

CREATE INDEX IF NOT EXISTS ix_food_name_trgm
ON catalog_dev.food USING gin (display_name_norm gin_trgm_ops);

CREATE TABLE IF NOT EXISTS catalog_dev.food_alias (
  food_alias_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  food_id uuid NOT NULL REFERENCES catalog_dev.food(food_id) ON DELETE CASCADE,

  alias text NOT NULL,
  alias_norm text GENERATED ALWAYS AS (catalog_dev.norm_text(alias)) STORED,
  locale text NOT NULL DEFAULT 'en',

  source text NOT NULL DEFAULT 'user',

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (food_id, alias_norm, locale)
);

DROP TRIGGER IF EXISTS trg_food_alias_updated_at ON catalog_dev.food_alias;
CREATE TRIGGER trg_food_alias_updated_at
BEFORE UPDATE ON catalog_dev.food_alias
FOR EACH ROW EXECUTE FUNCTION catalog_dev.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_food_alias_trgm
ON catalog_dev.food_alias USING gin (alias_norm gin_trgm_ops);

CREATE TABLE IF NOT EXISTS catalog_dev.nutrient (
  nutrient_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nutrient_key text NOT NULL,
  display_name text NOT NULL,
  unit text NOT NULL,
  category text NOT NULL DEFAULT 'other',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (nutrient_key)
);

DROP TRIGGER IF EXISTS trg_nutrient_updated_at ON catalog_dev.nutrient;
CREATE TRIGGER trg_nutrient_updated_at
BEFORE UPDATE ON catalog_dev.nutrient
FOR EACH ROW EXECUTE FUNCTION catalog_dev.tg_set_updated_at();

CREATE TABLE IF NOT EXISTS catalog_dev.food_nutrient (
  food_id uuid NOT NULL REFERENCES catalog_dev.food(food_id) ON DELETE CASCADE,
  nutrient_id uuid NOT NULL REFERENCES catalog_dev.nutrient(nutrient_id) ON DELETE CASCADE,
  basis text NOT NULL,
  amount numeric(16,6) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_food_nutrient_basis CHECK (basis IN ('per_100g','per_serving','per_unit')),
  PRIMARY KEY (food_id, nutrient_id, basis)
);

CREATE INDEX IF NOT EXISTS ix_food_nutrient_nutrient
ON catalog_dev.food_nutrient (nutrient_id);

CREATE TABLE IF NOT EXISTS catalog_dev.food_portion (
  portion_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  food_id uuid NOT NULL REFERENCES catalog_dev.food(food_id) ON DELETE CASCADE,

  label text NOT NULL,
  label_norm text GENERATED ALWAYS AS (catalog_dev.norm_text(label)) STORED,
  gram_weight numeric(12,3) NOT NULL,

  source text NOT NULL DEFAULT 'usda_fdc',

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (food_id, label_norm, gram_weight)
);

DROP TRIGGER IF EXISTS trg_food_portion_updated_at ON catalog_dev.food_portion;
CREATE TRIGGER trg_food_portion_updated_at
BEFORE UPDATE ON catalog_dev.food_portion
FOR EACH ROW EXECUTE FUNCTION catalog_dev.tg_set_updated_at();

COMMIT;
