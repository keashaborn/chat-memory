BEGIN;

-- Fix missing default_log for dead hang (mobility hold)
UPDATE catalog_dev.exercise
SET default_log = '{
  "type": "mobility",
  "fields": ["sets", "hold_s"],
  "optional": ["grip", "notes"]
}'::jsonb
WHERE slug = 'dead_hang'
  AND (default_log IS NULL OR default_log = '{}'::jsonb);

-- Fix strength/isometric holds to record sets + hold_s
UPDATE catalog_dev.exercise
SET default_log = '{
  "type": "strength",
  "fields": ["sets", "hold_s"],
  "optional": ["notes"]
}'::jsonb
WHERE slug IN ('plank','side_plank','hollow_hold','superman_hold')
  AND (default_log IS NULL OR default_log = '{}'::jsonb);

-- Aliases (idempotent)
WITH e AS (
  SELECT exercise_id, slug
  FROM catalog_dev.exercise
  WHERE slug IN ('dead_hang','plank','side_plank','hollow_hold','superman_hold')
)
INSERT INTO catalog_dev.exercise_alias (exercise_id, alias, locale, source)
SELECT e.exercise_id, x.alias, 'en', 'seed'
FROM e
JOIN (
  VALUES
    ('dead_hang','dead hang'),
    ('dead_hang','deadhang'),
    ('dead_hang','bar hang'),
    ('dead_hang','passive hang'),
    ('dead_hang','active hang'),
    ('dead_hang','dead hang hold'),

    ('plank','plank hold'),
    ('plank','front plank'),
    ('plank','forearm plank'),

    ('side_plank','side plank hold'),
    ('side_plank','side plank left'),
    ('side_plank','side plank right'),

    ('hollow_hold','hollow hold'),
    ('hollow_hold','hollow body hold'),

    ('superman_hold','superman hold'),
    ('superman_hold','superman')
) AS x(slug, alias)
  ON x.slug = e.slug
ON CONFLICT DO NOTHING;

COMMIT;
