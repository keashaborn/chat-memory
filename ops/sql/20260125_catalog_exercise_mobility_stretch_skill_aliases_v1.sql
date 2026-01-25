BEGIN;

-- 1) Add missing canonical exercise(s)
INSERT INTO catalog_dev.exercise (slug, display_name, kind, modality, movement_pattern)
VALUES ('dead_hang', 'Dead Hang', 'mobility', 'bodyweight', 'hang')
ON CONFLICT (slug) DO NOTHING;

-- 2) Alias helper: insert aliases by slug
-- Uses uq_exercise_alias_exercise_alias_locale for idempotency.

-- ---- stretch aliases (currently 0) ----
INSERT INTO catalog_dev.exercise_alias (exercise_id, alias, locale, source)
SELECT e.exercise_id, x.alias, 'en', 'seed'
FROM catalog_dev.exercise e
JOIN (VALUES
  ('pigeon_pose', 'pigeon stretch'),
  ('pigeon_pose', 'pigeon'),
  ('pigeon_pose', 'pigeon pose stretch'),

  ('couch_stretch', 'hip flexor stretch'),
  ('couch_stretch', 'couch stretch'),

  ('hamstring_stretch_supine', 'hamstring stretch'),
  ('hamstring_stretch_supine', 'supine hamstring stretch'),

  ('pec_stretch_doorway', 'doorway pec stretch'),
  ('pec_stretch_doorway', 'pec stretch doorway'),

  ('lat_stretch_overhead', 'overhead lat stretch'),
  ('lat_stretch_overhead', 'lat stretch'),

  ('seated_spinal_twist', 'seated twist'),
  ('seated_spinal_twist', 'spinal twist'),

  ('child_pose', 'childs pose'),
  ('child_pose', 'child pose')
) AS x(slug, alias) ON x.slug = e.slug
ON CONFLICT ON CONSTRAINT uq_exercise_alias_exercise_alias_locale DO NOTHING;

-- ---- mobility aliases ----
INSERT INTO catalog_dev.exercise_alias (exercise_id, alias, locale, source)
SELECT e.exercise_id, x.alias, 'en', 'seed'
FROM catalog_dev.exercise e
JOIN (VALUES
  ('cat_cow', 'cat cow'),
  ('cat_cow', 'cat-cow'),

  ('thoracic_rotation_open_book', 'open book'),
  ('thoracic_rotation_open_book', 'thoracic rotation'),

  ('worlds_greatest_stretch', 'worlds greatest stretch'),
  ('worlds_greatest_stretch', 'wgs'),

  ('spiderman_lunge_rotation', 'spiderman stretch'),
  ('spiderman_lunge_rotation', 'spiderman lunge'),

  ('ankle_dorsiflexion_rock', 'knee to wall'),
  ('ankle_dorsiflexion_rock', 'ankle rocks'),

  ('arm_circles', 'shoulder circles'),

  ('inchworm', 'inchworms'),

  ('foam_roll_quads', 'foam rolling quads'),
  ('foam_roll_hamstrings', 'foam rolling hamstrings'),
  ('foam_roll_calves', 'foam rolling calves'),
  ('foam_roll_tspine', 'foam rolling thoracic spine')
) AS x(slug, alias) ON x.slug = e.slug
ON CONFLICT ON CONSTRAINT uq_exercise_alias_exercise_alias_locale DO NOTHING;

-- ---- skill aliases ----
INSERT INTO catalog_dev.exercise_alias (exercise_id, alias, locale, source)
SELECT e.exercise_id, x.alias, 'en', 'seed'
FROM catalog_dev.exercise e
JOIN (VALUES
  ('box_jump', 'box jumps'),
  ('broad_jump', 'standing long jump'),
  ('broad_jump', 'broad jumps'),
  ('lateral_bounds', 'skater bounds'),
  ('pogo_hops', 'ankle hops')
) AS x(slug, alias) ON x.slug = e.slug
ON CONFLICT ON CONSTRAINT uq_exercise_alias_exercise_alias_locale DO NOTHING;

-- ---- dead hang aliases ----
INSERT INTO catalog_dev.exercise_alias (exercise_id, alias, locale, source)
SELECT e.exercise_id, x.alias, 'en', 'seed'
FROM catalog_dev.exercise e
JOIN (VALUES
  ('dead_hang', 'dead hang'),
  ('dead_hang', 'passive hang'),
  ('dead_hang', 'bar hang')
) AS x(slug, alias) ON x.slug = e.slug
ON CONFLICT ON CONSTRAINT uq_exercise_alias_exercise_alias_locale DO NOTHING;

COMMIT;
