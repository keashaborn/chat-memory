BEGIN;

-- Helpers
CREATE OR REPLACE FUNCTION catalog_dev.escape_like(t text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
  SELECT replace(replace(replace(t, E'\\', E'\\\\'), '%', E'\\%'), '_', E'\\_')
$$;

-- Exercise search
CREATE OR REPLACE FUNCTION catalog_dev.search_exercises(
  q text,
  max_results int DEFAULT 25,
  p_locale text DEFAULT 'en'
)
RETURNS TABLE (
  exercise_id uuid,
  display_name text,
  kind text,
  modality text,
  score real,
  matched_text text,
  matched_source text,
  brand_name text,
  model_name text
)
LANGUAGE sql
STABLE
AS $$
WITH qn AS (
  SELECT
    catalog_dev.norm_text(q) AS qn,
    catalog_dev.escape_like(catalog_dev.norm_text(q)) AS q_like
),
cand_ex AS (
  SELECT
    e.exercise_id,
    e.display_name,
    e.kind,
    e.modality,
    similarity(e.display_name_norm, qn.qn) AS score,
    e.display_name AS matched_text,
    'canonical'::text AS matched_source,
    NULL::text AS brand_name,
    NULL::text AS model_name
  FROM catalog_dev.exercise e, qn
  WHERE e.is_active
    AND (
      e.display_name_norm % qn.qn
      OR e.display_name_norm LIKE ('%' || qn.q_like || '%') ESCAPE E'\\'
    )
),
cand_alias AS (
  SELECT
    ea.exercise_id,
    e.display_name,
    e.kind,
    e.modality,
    similarity(ea.alias_norm, qn.qn) AS score,
    ea.alias AS matched_text,
    'alias'::text AS matched_source,
    b.brand_name AS brand_name,
    ea.model_name
  FROM catalog_dev.exercise_alias ea
  JOIN catalog_dev.exercise e ON e.exercise_id = ea.exercise_id
  LEFT JOIN catalog_dev.equipment_brand b ON b.brand_id = ea.brand_id,
  qn
  WHERE e.is_active
    AND ea.locale = p_locale
    AND (
      ea.alias_norm % qn.qn
      OR ea.alias_norm LIKE ('%' || qn.q_like || '%') ESCAPE E'\\'
    )
),
all_cand AS (
  SELECT * FROM cand_ex
  UNION ALL
  SELECT * FROM cand_alias
),
ranked AS (
  SELECT
    *,
    row_number() OVER (PARTITION BY exercise_id ORDER BY score DESC, matched_source DESC) AS rn
  FROM all_cand
)
SELECT
  exercise_id, display_name, kind, modality,
  score::real, matched_text, matched_source, brand_name, model_name
FROM ranked
WHERE rn = 1
ORDER BY score DESC, display_name
LIMIT max_results;
$$;

-- Food search
CREATE OR REPLACE FUNCTION catalog_dev.search_foods(
  q text,
  max_results int DEFAULT 25,
  p_locale text DEFAULT 'en'
)
RETURNS TABLE (
  food_id uuid,
  display_name text,
  brand text,
  barcode text,
  source text,
  basis text,
  kcal numeric(12,3),
  protein_g numeric(12,3),
  carbs_g numeric(12,3),
  fat_g numeric(12,3),
  score real,
  matched_text text,
  matched_source text
)
LANGUAGE sql
STABLE
AS $$
WITH qn AS (
  SELECT
    catalog_dev.norm_text(q) AS qn,
    catalog_dev.escape_like(catalog_dev.norm_text(q)) AS q_like
),
cand_food AS (
  SELECT
    f.food_id, f.display_name, f.brand, f.barcode, f.source, f.basis,
    f.kcal, f.protein_g, f.carbs_g, f.fat_g,
    similarity(f.display_name_norm, qn.qn) AS score,
    f.display_name AS matched_text,
    'canonical'::text AS matched_source
  FROM catalog_dev.food f, qn
  WHERE f.is_active
    AND (
      f.display_name_norm % qn.qn
      OR f.display_name_norm LIKE ('%' || qn.q_like || '%') ESCAPE E'\\'
    )
),
cand_alias AS (
  SELECT
    fa.food_id, f.display_name, f.brand, f.barcode, f.source, f.basis,
    f.kcal, f.protein_g, f.carbs_g, f.fat_g,
    similarity(fa.alias_norm, qn.qn) AS score,
    fa.alias AS matched_text,
    'alias'::text AS matched_source
  FROM catalog_dev.food_alias fa
  JOIN catalog_dev.food f ON f.food_id = fa.food_id, qn
  WHERE f.is_active
    AND fa.locale = p_locale
    AND (
      fa.alias_norm % qn.qn
      OR fa.alias_norm LIKE ('%' || qn.q_like || '%') ESCAPE E'\\'
    )
),
all_cand AS (
  SELECT * FROM cand_food
  UNION ALL
  SELECT * FROM cand_alias
),
ranked AS (
  SELECT
    *,
    row_number() OVER (PARTITION BY food_id ORDER BY score DESC, matched_source DESC) AS rn
  FROM all_cand
)
SELECT
  food_id, display_name, brand, barcode, source, basis,
  kcal, protein_g, carbs_g, fat_g,
  score::real, matched_text, matched_source
FROM ranked
WHERE rn = 1
ORDER BY score DESC, display_name
LIMIT max_results;
$$;

-- Seed brands (idempotent)
INSERT INTO catalog_dev.equipment_brand (brand_name)
VALUES ('Hammer Strength'),('Life Fitness'),('Cybex'),('Technogym'),('Precor'),('Matrix')
ON CONFLICT (brand_name_norm)
DO UPDATE SET brand_name = EXCLUDED.brand_name;

-- Seed canonical exercises/activities (idempotent)
INSERT INTO catalog_dev.exercise
  (slug, display_name, kind, modality, movement_pattern, primary_muscles, secondary_muscles, joints, equipment_required, unilateral, default_log, source)
VALUES
  ('barbell_back_squat','Barbell Back Squat','strength','free_weight','squat',
    ARRAY['quadriceps','glutes'], ARRAY['hamstrings','erectors'], ARRAY['knee','hip'], ARRAY['barbell','rack'], false,
    '{"type":"strength","fields":["sets","reps","load"]}'::jsonb,'seed'),

  ('plate_loaded_chest_press','Chest Press (Plate-Loaded)','strength','machine_plate_loaded','horizontal_push',
    ARRAY['chest'], ARRAY['triceps','anterior_deltoid'], ARRAY['shoulder','elbow'], ARRAY['plate_loaded_press'], false,
    '{"type":"strength","fields":["sets","reps","load"]}'::jsonb,'seed'),

  ('selectorized_chest_press','Chest Press (Selectorized)','strength','machine_selectorized','horizontal_push',
    ARRAY['chest'], ARRAY['triceps','anterior_deltoid'], ARRAY['shoulder','elbow'], ARRAY['chest_press_machine'], false,
    '{"type":"strength","fields":["sets","reps","stack_load"]}'::jsonb,'seed'),

  ('selectorized_lat_pulldown','Lat Pulldown (Selectorized)','strength','machine_selectorized','vertical_pull',
    ARRAY['lats'], ARRAY['biceps','mid_back'], ARRAY['shoulder','elbow'], ARRAY['lat_pulldown_machine'], false,
    '{"type":"strength","fields":["sets","reps","stack_load"]}'::jsonb,'seed'),

  ('seated_row_machine','Seated Row (Selectorized)','strength','machine_selectorized','horizontal_pull',
    ARRAY['mid_back'], ARRAY['biceps','rear_delts'], ARRAY['shoulder','elbow'], ARRAY['row_machine'], false,
    '{"type":"strength","fields":["sets","reps","stack_load"]}'::jsonb,'seed'),

  ('cable_triceps_pushdown','Cable Triceps Pushdown','strength','cable','elbow_extension',
    ARRAY['triceps'], ARRAY[]::text[], ARRAY['elbow'], ARRAY['cable_station'], false,
    '{"type":"strength","fields":["sets","reps","load"]}'::jsonb,'seed'),

  ('treadmill_run','Treadmill Run','cardio','cardio_machine','locomotion',
    ARRAY[]::text[], ARRAY[]::text[], ARRAY[]::text[], ARRAY['treadmill'], false,
    '{"type":"cardio","fields":["duration_s","distance_m"],"optional":["avg_hr","calories_kcal"]}'::jsonb,'seed')
ON CONFLICT (slug)
DO UPDATE SET
  display_name = EXCLUDED.display_name,
  kind = EXCLUDED.kind,
  modality = EXCLUDED.modality,
  movement_pattern = EXCLUDED.movement_pattern,
  primary_muscles = EXCLUDED.primary_muscles,
  secondary_muscles = EXCLUDED.secondary_muscles,
  joints = EXCLUDED.joints,
  equipment_required = EXCLUDED.equipment_required,
  unilateral = EXCLUDED.unilateral,
  default_log = EXCLUDED.default_log,
  source = EXCLUDED.source;

-- Seed exercise aliases (no CTE reuse; each insert resolves brand_id + exercise_id inline)
INSERT INTO catalog_dev.exercise_alias (exercise_id, alias, locale, brand_id, model_name, source, confidence)
SELECT e.exercise_id, v.alias, 'en',
       (SELECT brand_id FROM catalog_dev.equipment_brand WHERE brand_name_norm = catalog_dev.norm_text('Hammer Strength')),
       NULL, 'seed', 0.95
FROM catalog_dev.exercise e
JOIN (VALUES
  ('Hammer Strength Iso-Lateral Bench Press'),
  ('Hammer Strength Bench Press'),
  ('Hammer Strength Chest Press')
) v(alias) ON true
WHERE e.slug = 'plate_loaded_chest_press'
ON CONFLICT (exercise_id, alias_norm, locale) DO NOTHING;

INSERT INTO catalog_dev.exercise_alias (exercise_id, alias, locale, brand_id, model_name, source, confidence)
SELECT e.exercise_id, v.alias, 'en',
       (SELECT brand_id FROM catalog_dev.equipment_brand WHERE brand_name_norm = catalog_dev.norm_text('Technogym')),
       NULL, 'seed', 0.85
FROM catalog_dev.exercise e
JOIN (VALUES
  ('Technogym Chest Press'),
  ('Technogym Selection Chest Press')
) v(alias) ON true
WHERE e.slug = 'selectorized_chest_press'
ON CONFLICT (exercise_id, alias_norm, locale) DO NOTHING;

INSERT INTO catalog_dev.exercise_alias (exercise_id, alias, locale, brand_id, model_name, source, confidence)
SELECT e.exercise_id, v.alias, 'en',
       NULL, NULL, 'seed', 0.90
FROM catalog_dev.exercise e
JOIN (VALUES
  ('Lat Pulldown'),
  ('Lat Pull Down'),
  ('Pulldown Machine')
) v(alias) ON true
WHERE e.slug = 'selectorized_lat_pulldown'
ON CONFLICT (exercise_id, alias_norm, locale) DO NOTHING;

INSERT INTO catalog_dev.exercise_alias (exercise_id, alias, locale, brand_id, model_name, source, confidence)
SELECT e.exercise_id, v.alias, 'en',
       NULL, NULL, 'seed', 0.85
FROM catalog_dev.exercise e
JOIN (VALUES
  ('Seated Row'),
  ('Low Row Machine')
) v(alias) ON true
WHERE e.slug = 'seated_row_machine'
ON CONFLICT (exercise_id, alias_norm, locale) DO NOTHING;

INSERT INTO catalog_dev.exercise_alias (exercise_id, alias, locale, brand_id, model_name, source, confidence)
SELECT e.exercise_id, v.alias, 'en',
       NULL, NULL, 'seed', 0.90
FROM catalog_dev.exercise e
JOIN (VALUES
  ('Triceps Pushdown'),
  ('Cable Pushdown'),
  ('Rope Pushdown')
) v(alias) ON true
WHERE e.slug = 'cable_triceps_pushdown'
ON CONFLICT (exercise_id, alias_norm, locale) DO NOTHING;

-- Seed foods + aliases (plumbing only)
INSERT INTO catalog_dev.food (display_name, source, source_id, basis, data)
VALUES
  ('Egg, whole, raw', 'manual', 'seed:egg_whole_raw', 'per_100g', '{"seed":true}'::jsonb),
  ('Chicken breast, cooked', 'manual', 'seed:chicken_breast_cooked', 'per_100g', '{"seed":true}'::jsonb),
  ('Greek yogurt, plain', 'manual', 'seed:greek_yogurt_plain', 'per_100g', '{"seed":true}'::jsonb)
ON CONFLICT (source, source_id)
DO UPDATE SET display_name = EXCLUDED.display_name, basis = EXCLUDED.basis, data = EXCLUDED.data;

INSERT INTO catalog_dev.food_alias (food_id, alias, locale, source)
SELECT f.food_id, v.alias, 'en', 'seed'
FROM catalog_dev.food f
JOIN (VALUES
  ('seed:egg_whole_raw','whole egg'),
  ('seed:egg_whole_raw','egg'),
  ('seed:chicken_breast_cooked','chicken breast'),
  ('seed:greek_yogurt_plain','greek yogurt'),
  ('seed:greek_yogurt_plain','yogurt')
) v(source_id, alias)
ON f.source = 'manual' AND f.source_id = v.source_id
ON CONFLICT DO NOTHING;

COMMIT;
