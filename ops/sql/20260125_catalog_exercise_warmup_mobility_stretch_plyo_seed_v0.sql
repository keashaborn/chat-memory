BEGIN;

-- -------------------------------------------------------------------
-- Seed pack v0: warm-up, mobility, stretch, SMR/release, plyometrics
-- Idempotent: upserts by slug; aliases insert with ON CONFLICT DO NOTHING.
-- -------------------------------------------------------------------

WITH seed AS (
  SELECT *
  FROM (VALUES
    -- ----------------
    -- MOBILITY / WARMUP (controlled ROM / prep)
    -- ----------------
    ('cat_cow','Cat-Cow','mobility','bodyweight','spine_mobility', ARRAY[]::text[], '{"type":"mobility","fields":["reps"],"optional":["notes"]}'::jsonb),
    ('worlds_greatest_stretch','World’s Greatest Stretch','mobility','bodyweight','dynamic_lunge', ARRAY[]::text[], '{"type":"mobility","fields":["reps"],"optional":["side","notes"]}'::jsonb),
    ('inchworm','Inchworm','mobility','bodyweight','hinge_walkout', ARRAY[]::text[], '{"type":"mobility","fields":["reps"],"optional":["notes"]}'::jsonb),
    ('spiderman_lunge_rotation','Spiderman Lunge + Rotation','mobility','bodyweight','lunge_rotation', ARRAY[]::text[], '{"type":"mobility","fields":["reps"],"optional":["side","notes"]}'::jsonb),
    ('thoracic_rotation_open_book','Open Book (Thoracic Rotation)','mobility','bodyweight','tspine_rotation', ARRAY[]::text[], '{"type":"mobility","fields":["reps"],"optional":["side","notes"]}'::jsonb),
    ('ankle_dorsiflexion_rock','Ankle Dorsiflexion Rock','mobility','bodyweight','ankle_mobility', ARRAY[]::text[], '{"type":"mobility","fields":["reps"],"optional":["side","notes"]}'::jsonb),
    ('leg_swing_front_back','Leg Swing (Front/Back)','mobility','bodyweight','hip_mobility', ARRAY[]::text[], '{"type":"mobility","fields":["reps"],"optional":["side"]}'::jsonb),
    ('leg_swing_side','Leg Swing (Side)','mobility','bodyweight','hip_mobility', ARRAY[]::text[], '{"type":"mobility","fields":["reps"],"optional":["side"]}'::jsonb),
    ('arm_circles','Arm Circles','mobility','bodyweight','shoulder_mobility', ARRAY[]::text[], '{"type":"mobility","fields":["reps"],"optional":["direction"]}'::jsonb),
    ('scapular_wall_slide','Scapular Wall Slide','mobility','bodyweight','scap_control', ARRAY[]::text[], '{"type":"mobility","fields":["reps"],"optional":["notes"]}'::jsonb),

    -- ----------------
    -- ACTIVATION (strength-ish warmups)
    -- ----------------
    ('glute_bridge','Glute Bridge','strength','bodyweight','hip_extension', ARRAY[]::text[], '{"type":"strength","fields":["sets","reps"],"optional":["tempo","notes"]}'::jsonb),
    ('single_leg_glute_bridge','Single-Leg Glute Bridge','strength','bodyweight','hip_extension', ARRAY[]::text[], '{"type":"strength","fields":["sets","reps"],"optional":["side","tempo"]}'::jsonb),
    ('clamshell','Clamshell','strength','bodyweight','hip_abduction', ARRAY['miniband']::text[], '{"type":"strength","fields":["sets","reps"],"optional":["side","notes"]}'::jsonb),
    ('lateral_band_walk','Lateral Band Walk','strength','other','hip_abduction', ARRAY['miniband']::text[], '{"type":"strength","fields":["sets","steps"],"optional":["notes"]}'::jsonb),
    ('monster_walk','Monster Walk','strength','other','hip_abduction', ARRAY['miniband']::text[], '{"type":"strength","fields":["sets","steps"],"optional":["notes"]}'::jsonb),
    ('band_pull_apart','Band Pull-Apart','strength','other','scap_retraction', ARRAY['resistance_band']::text[], '{"type":"strength","fields":["sets","reps"],"optional":["notes"]}'::jsonb),
    ('scapular_push_up','Scapular Push-Up','strength','bodyweight','scap_protraction', ARRAY[]::text[], '{"type":"strength","fields":["sets","reps"],"optional":["notes"]}'::jsonb),

    -- ----------------
    -- SMR / RELEASE (duration-based)
    -- ----------------
    ('foam_roll_quads','Foam Roll: Quads','mobility','other','smr', ARRAY['foam_roller']::text[], '{"type":"mobility","fields":["duration_s"],"optional":["side","notes"]}'::jsonb),
    ('foam_roll_hamstrings','Foam Roll: Hamstrings','mobility','other','smr', ARRAY['foam_roller']::text[], '{"type":"mobility","fields":["duration_s"],"optional":["side","notes"]}'::jsonb),
    ('foam_roll_glutes','Foam Roll: Glutes','mobility','other','smr', ARRAY['foam_roller']::text[], '{"type":"mobility","fields":["duration_s"],"optional":["side","notes"]}'::jsonb),
    ('foam_roll_calves','Foam Roll: Calves','mobility','other','smr', ARRAY['foam_roller']::text[], '{"type":"mobility","fields":["duration_s"],"optional":["side","notes"]}'::jsonb),
    ('foam_roll_tspine','Foam Roll: Thoracic Spine','mobility','other','smr', ARRAY['foam_roller']::text[], '{"type":"mobility","fields":["duration_s"],"optional":["notes"]}'::jsonb),
    ('lacrosse_ball_pec','Lacrosse Ball: Pec Release','mobility','other','smr', ARRAY['lacrosse_ball']::text[], '{"type":"mobility","fields":["duration_s"],"optional":["side","notes"]}'::jsonb),
    ('lacrosse_ball_foot','Lacrosse Ball: Foot Release','mobility','other','smr', ARRAY['lacrosse_ball']::text[], '{"type":"mobility","fields":["duration_s"],"optional":["side","notes"]}'::jsonb),

    -- ----------------
    -- STRETCH (hold-based)
    -- ----------------
    ('couch_stretch','Couch Stretch','stretch','bodyweight','hip_flexor', ARRAY[]::text[], '{"type":"stretch","fields":["hold_s"],"optional":["side","notes"]}'::jsonb),
    ('pigeon_pose','Pigeon Pose','stretch','bodyweight','glute_stretch', ARRAY[]::text[], '{"type":"stretch","fields":["hold_s"],"optional":["side","notes"]}'::jsonb),
    ('hamstring_stretch_supine','Hamstring Stretch (Supine)','stretch','bodyweight','hamstring_stretch', ARRAY[]::text[], '{"type":"stretch","fields":["hold_s"],"optional":["side"]}'::jsonb),
    ('calf_stretch_wall','Calf Stretch (Wall)','stretch','bodyweight','calf_stretch', ARRAY[]::text[], '{"type":"stretch","fields":["hold_s"],"optional":["side"]}'::jsonb),
    ('pec_stretch_doorway','Doorway Pec Stretch','stretch','bodyweight','pec_stretch', ARRAY[]::text[], '{"type":"stretch","fields":["hold_s"],"optional":["side"]}'::jsonb),
    ('lat_stretch_overhead','Lat Stretch (Overhead)','stretch','bodyweight','lat_stretch', ARRAY[]::text[], '{"type":"stretch","fields":["hold_s"],"optional":["side"]}'::jsonb),
    ('child_pose','Child’s Pose','stretch','bodyweight','spine_flexion', ARRAY[]::text[], '{"type":"stretch","fields":["hold_s"],"optional":["notes"]}'::jsonb),
    ('seated_spinal_twist','Seated Spinal Twist','stretch','bodyweight','spine_rotation', ARRAY[]::text[], '{"type":"stretch","fields":["hold_s"],"optional":["side","notes"]}'::jsonb),

    -- ----------------
    -- PLYOMETRICS (box jumps etc)
    -- ----------------
    ('box_jump','Box Jump','skill','bodyweight','jump', ARRAY['box']::text[], '{"type":"skill","fields":["sets","reps"],"optional":["height","notes"]}'::jsonb),
    ('broad_jump','Broad Jump','skill','bodyweight','jump', ARRAY[]::text[], '{"type":"skill","fields":["sets","reps"],"optional":["notes"]}'::jsonb),
    ('pogo_hops','Pogo Hops','skill','bodyweight','jump', ARRAY[]::text[], '{"type":"skill","fields":["sets","reps"],"optional":["notes"]}'::jsonb),
    ('lateral_bounds','Lateral Bounds','skill','bodyweight','jump', ARRAY[]::text[], '{"type":"skill","fields":["sets","reps"],"optional":["side","notes"]}'::jsonb),

    -- ----------------
    -- QUICK CARDIO WARMUPS (duration)
    -- ----------------
    ('jumping_jacks','Jumping Jacks','cardio','cardio_outdoor','locomotion', ARRAY[]::text[], '{"type":"cardio","fields":["duration_s"],"optional":["calories_kcal","avg_hr"]}'::jsonb),
    ('high_knees','High Knees','cardio','cardio_outdoor','locomotion', ARRAY[]::text[], '{"type":"cardio","fields":["duration_s"],"optional":["notes"]}'::jsonb),
    ('butt_kicks','Butt Kicks','cardio','cardio_outdoor','locomotion', ARRAY[]::text[], '{"type":"cardio","fields":["duration_s"],"optional":["notes"]}'::jsonb)
  ) AS t(
    slug, display_name, kind, modality, movement_pattern, equipment_required, default_log
  )
)
INSERT INTO catalog_dev.exercise (slug, display_name, kind, modality, movement_pattern, equipment_required, default_log, source)
SELECT
  slug,
  display_name,
  kind,
  modality,
  movement_pattern,
  equipment_required,
  default_log,
  'seed'
FROM seed
ON CONFLICT (slug) DO UPDATE
SET display_name = EXCLUDED.display_name,
    kind = EXCLUDED.kind,
    modality = EXCLUDED.modality,
    movement_pattern = EXCLUDED.movement_pattern,
    equipment_required = EXCLUDED.equipment_required,
    default_log = EXCLUDED.default_log,
    is_active = true,
    is_public = true,
    updated_at = now();

-- Aliases (minimal; expand later)
-- Box jump synonyms
INSERT INTO catalog_dev.exercise_alias (exercise_id, alias, locale, source)
SELECT e.exercise_id, a.alias, 'en', 'seed'
FROM catalog_dev.exercise e
JOIN (VALUES
  ('box_jump','box jumps'),
  ('box_jump','box jump'),
  ('box_jump','plyo box jump')
) AS a(slug, alias) ON a.slug = e.slug
ON CONFLICT DO NOTHING;

-- World’s Greatest Stretch synonyms
INSERT INTO catalog_dev.exercise_alias (exercise_id, alias, locale, source)
SELECT e.exercise_id, a.alias, 'en', 'seed'
FROM catalog_dev.exercise e
JOIN (VALUES
  ('worlds_greatest_stretch','worlds greatest stretch'),
  ('worlds_greatest_stretch','greatest stretch'),
  ('worlds_greatest_stretch','spiderman stretch')
) AS a(slug, alias) ON a.slug = e.slug
ON CONFLICT DO NOTHING;

COMMIT;
