BEGIN;

-- Additive browse taxonomy for the global exercise catalog.
-- Existing catalog exercises, aliases, personal exercises, templates, sessions,
-- sets, observations, and conditioning logs are intentionally untouched.

CREATE TABLE IF NOT EXISTS catalog_dev.exercise_family (
  exercise_family_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug text NOT NULL UNIQUE,
  display_name text NOT NULL,
  kind text NOT NULL DEFAULT 'strength',
  movement_group text NOT NULL,
  movement_pattern text NOT NULL,
  primary_muscles text[] NOT NULL DEFAULT '{}'::text[],
  description text NOT NULL DEFAULT '',
  sort_order integer NOT NULL DEFAULT 100,
  is_active boolean NOT NULL DEFAULT true,
  source text NOT NULL DEFAULT 'system_seed_v1',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_exercise_family_slug_lower CHECK (slug = lower(slug)),
  CONSTRAINT ck_exercise_family_kind CHECK (
    kind IN ('strength', 'cardio', 'mobility', 'stretch', 'skill', 'other')
  )
);

DROP TRIGGER IF EXISTS trg_exercise_family_updated_at
  ON catalog_dev.exercise_family;
CREATE TRIGGER trg_exercise_family_updated_at
BEFORE UPDATE ON catalog_dev.exercise_family
FOR EACH ROW EXECUTE FUNCTION catalog_dev.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_exercise_family_browse
  ON catalog_dev.exercise_family (
    is_active,
    kind,
    movement_group,
    sort_order,
    lower(display_name)
  );

CREATE TABLE IF NOT EXISTS catalog_dev.exercise_family_member (
  exercise_family_member_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  exercise_family_id uuid NOT NULL
    REFERENCES catalog_dev.exercise_family(exercise_family_id)
    ON DELETE RESTRICT,
  exercise_id uuid NOT NULL
    REFERENCES catalog_dev.exercise(exercise_id)
    ON DELETE RESTRICT,
  variant_label text NOT NULL,
  is_default boolean NOT NULL DEFAULT false,
  sort_order integer NOT NULL DEFAULT 100,
  is_active boolean NOT NULL DEFAULT true,
  source text NOT NULL DEFAULT 'system_seed_v1',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_exercise_family_member UNIQUE (exercise_family_id, exercise_id)
);

DROP TRIGGER IF EXISTS trg_exercise_family_member_updated_at
  ON catalog_dev.exercise_family_member;
CREATE TRIGGER trg_exercise_family_member_updated_at
BEFORE UPDATE ON catalog_dev.exercise_family_member
FOR EACH ROW EXECUTE FUNCTION catalog_dev.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_exercise_family_member_browse
  ON catalog_dev.exercise_family_member (
    exercise_family_id,
    is_active,
    is_default DESC,
    sort_order
  );

REVOKE ALL ON TABLE
  catalog_dev.exercise_family,
  catalog_dev.exercise_family_member
FROM PUBLIC;

GRANT SELECT ON TABLE
  catalog_dev.exercise_family,
  catalog_dev.exercise_family_member
TO brains_app;

COMMENT ON TABLE catalog_dev.exercise_family IS
  'Generic browse concepts. Additive taxonomy; not a replacement for exercise_id.';
COMMENT ON TABLE catalog_dev.exercise_family_member IS
  'Maps generic browse concepts to existing equipment-specific catalog exercises.';

INSERT INTO catalog_dev.exercise_family (
  slug,
  display_name,
  kind,
  movement_group,
  movement_pattern,
  primary_muscles,
  description,
  sort_order,
  source
)
VALUES
  ('bench_press', 'Bench Press', 'strength', 'chest', 'horizontal_push',
    ARRAY['chest'], 'Flat, incline, decline, free-weight, Smith, and machine pressing.', 10, 'system_seed_v1'),
  ('chest_fly', 'Chest Fly', 'strength', 'chest', 'horizontal_adduction',
    ARRAY['chest'], 'Cable, pec-deck, and machine fly variations.', 20, 'system_seed_v1'),
  ('push_up', 'Push-Up', 'strength', 'chest', 'horizontal_push',
    ARRAY['chest'], 'Bodyweight horizontal pressing.', 30, 'system_seed_v1'),
  ('row', 'Row', 'strength', 'back', 'horizontal_pull',
    ARRAY['mid_back', 'lats'], 'Free-weight and machine horizontal pulling.', 40, 'system_seed_v1'),
  ('lat_pulldown', 'Lat Pulldown', 'strength', 'back', 'vertical_pull',
    ARRAY['lats'], 'Cable and selectorized pulldown variations.', 50, 'system_seed_v1'),
  ('pull_up', 'Pull-Up', 'strength', 'back', 'vertical_pull',
    ARRAY['lats'], 'Assisted, bodyweight, and weighted pull-up variations.', 60, 'system_seed_v1'),
  ('pullover', 'Pullover', 'strength', 'back', 'pull_over',
    ARRAY['lats'], 'Machine and free-form pullover variations.', 70, 'system_seed_v1'),
  ('shoulder_press', 'Shoulder Press', 'strength', 'shoulders', 'vertical_push',
    ARRAY['delts'], 'Free-weight, Smith, selectorized, and plate-loaded overhead pressing.', 80, 'system_seed_v1'),
  ('lateral_raise', 'Lateral Raise', 'strength', 'shoulders', 'shoulder_abduction',
    ARRAY['delts'], 'Dumbbell, cable, and machine lateral-raise variations.', 90, 'system_seed_v1'),
  ('rear_delt', 'Rear Delt / Reverse Fly', 'strength', 'shoulders', 'horizontal_abduction',
    ARRAY['rear_delts'], 'Cable and machine rear-delt variations.', 100, 'system_seed_v1'),
  ('biceps_curl', 'Biceps Curl', 'strength', 'arms', 'elbow_flexion',
    ARRAY['biceps'], 'Barbell, dumbbell, cable, and machine curl variations.', 110, 'system_seed_v1'),
  ('triceps_extension', 'Triceps Extension', 'strength', 'arms', 'elbow_extension',
    ARRAY['triceps'], 'Pushdown and overhead extension variations.', 120, 'system_seed_v1'),
  ('dip', 'Dip', 'strength', 'arms', 'dip',
    ARRAY['triceps', 'chest'], 'Assisted, machine, bodyweight, and weighted dip variations.', 130, 'system_seed_v1'),
  ('squat', 'Squat', 'strength', 'legs', 'squat',
    ARRAY['quadriceps', 'glutes'], 'Free-weight and machine squat variations.', 140, 'system_seed_v1'),
  ('leg_press', 'Leg Press', 'strength', 'legs', 'squat_press',
    ARRAY['quadriceps', 'glutes'], 'Selectorized and plate-loaded leg-press patterns.', 150, 'system_seed_v1'),
  ('split_squat_lunge', 'Split Squat / Lunge', 'strength', 'legs', 'lunge',
    ARRAY['quadriceps', 'glutes'], 'Single-leg squat and lunge patterns.', 160, 'system_seed_v1'),
  ('deadlift', 'Deadlift / Hip Hinge', 'strength', 'glutes_hips', 'hinge',
    ARRAY['hamstrings', 'glutes'], 'Conventional and Romanian deadlift variations.', 170, 'system_seed_v1'),
  ('kettlebell_swing', 'Kettlebell Swing', 'strength', 'glutes_hips', 'hinge',
    ARRAY['glutes', 'hamstrings'], 'One-hand and two-hand kettlebell swings.', 180, 'system_seed_v1'),
  ('hip_thrust_bridge', 'Hip Thrust / Glute Bridge', 'strength', 'glutes_hips', 'hip_extension',
    ARRAY['glutes'], 'Bodyweight, barbell, and machine hip-extension variations.', 190, 'system_seed_v1'),
  ('leg_curl', 'Leg Curl', 'strength', 'legs', 'knee_flexion',
    ARRAY['hamstrings'], 'Seated, lying, kneeling, and Nordic curl variations.', 200, 'system_seed_v1'),
  ('leg_extension', 'Leg Extension', 'strength', 'legs', 'knee_extension',
    ARRAY['quadriceps'], 'Machine knee-extension variations.', 210, 'system_seed_v1'),
  ('calf_raise', 'Calf Raise', 'strength', 'legs', 'plantarflexion',
    ARRAY['calves'], 'Standing and seated calf-raise variations.', 220, 'system_seed_v1'),
  ('hip_abduction', 'Hip Abduction', 'strength', 'glutes_hips', 'hip_abduction',
    ARRAY['glutes'], 'Band, bodyweight, and machine hip-abduction variations.', 230, 'system_seed_v1'),
  ('hip_adduction', 'Hip Adduction', 'strength', 'glutes_hips', 'hip_adduction',
    ARRAY['adductors'], 'Machine hip-adduction variations.', 240, 'system_seed_v1'),
  ('core_flexion', 'Core Flexion', 'strength', 'core', 'flexion',
    ARRAY['abs'], 'Crunch, sit-up, and machine flexion variations.', 250, 'system_seed_v1'),
  ('core_stability', 'Core Stability', 'strength', 'core', 'anti_extension',
    ARRAY['core'], 'Plank, anti-extension, anti-rotation, and lateral-stability variations.', 260, 'system_seed_v1'),
  ('leg_raise', 'Leg Raise', 'strength', 'core', 'hip_flexion',
    ARRAY['abs', 'hip_flexors'], 'Hanging and lying leg-raise variations.', 270, 'system_seed_v1'),
  ('loaded_carry', 'Loaded Carry', 'strength', 'carries', 'carry',
    ARRAY['core', 'grip'], 'Farmer, rack, and suitcase carry variations.', 280, 'system_seed_v1')
ON CONFLICT (slug) DO UPDATE
SET
  display_name = EXCLUDED.display_name,
  kind = EXCLUDED.kind,
  movement_group = EXCLUDED.movement_group,
  movement_pattern = EXCLUDED.movement_pattern,
  primary_muscles = EXCLUDED.primary_muscles,
  description = EXCLUDED.description,
  sort_order = EXCLUDED.sort_order,
  source = EXCLUDED.source,
  is_active = true;

CREATE TEMP TABLE exercise_family_member_seed_v1 (
  family_slug text NOT NULL,
  exercise_slug text NOT NULL,
  sort_order integer NOT NULL,
  is_default boolean NOT NULL
) ON COMMIT DROP;

INSERT INTO exercise_family_member_seed_v1 (
  family_slug,
  exercise_slug,
  sort_order,
  is_default
)
VALUES
  ('bench_press', 'dumbbell_bench_press', 10, true),
  ('bench_press', 'kettlebell_floor_press', 20, false),
  ('bench_press', 'decline_dumbbell_press', 30, false),
  ('bench_press', 'incline_dumbbell_press', 40, false),
  ('bench_press', 'smith_machine_flat_bench_press', 50, false),
  ('bench_press', 'smith_machine_incline_bench_press', 60, false),
  ('bench_press', 'smith_machine_decline_bench_press', 70, false),
  ('bench_press', 'selectorized_chest_press', 80, false),
  ('bench_press', 'plate_loaded_chest_press', 90, false),
  ('bench_press', 'chest_press', 100, false),
  ('bench_press', 'bench_press', 110, false),
  ('bench_press', 'incline_bench_press', 120, false),
  ('bench_press', 'decline_bench_press', 130, false),
  ('bench_press', 'jammer_press_ground_base', 140, false),
  ('chest_fly', 'cable_fly', 10, true),
  ('chest_fly', 'pec_deck', 20, false),
  ('chest_fly', 'machine_fly', 30, false),
  ('chest_fly', 'arm_cross', 40, false),
  ('push_up', 'push_up', 10, true),
  ('push_up', 'scapular_push_up', 20, false),
  ('row', 'barbell_row', 10, true),
  ('row', 'one_arm_dumbbell_row', 20, false),
  ('row', 'kettlebell_row_one_arm', 30, false),
  ('row', 'seated_row', 40, false),
  ('row', 'seated_row_machine', 50, false),
  ('row', 't_bar_row', 60, false),
  ('lat_pulldown', 'lat_pulldown', 10, true),
  ('lat_pulldown', 'selectorized_lat_pulldown', 20, false),
  ('lat_pulldown', 'close_grip_lat_pulldown', 30, false),
  ('pull_up', 'wide_grip_pull_up', 10, true),
  ('pull_up', 'narrow_grip_pull_up', 20, false),
  ('pull_up', 'assisted_pull_up', 30, false),
  ('pull_up', 'weighted_pull_up', 40, false),
  ('pullover', 'pullover', 10, true),
  ('pullover', 'pullover_plate_loaded', 20, false),
  ('shoulder_press', 'overhead_press', 10, true),
  ('shoulder_press', 'dumbbell_shoulder_press', 20, false),
  ('shoulder_press', 'kettlebell_press_single', 30, false),
  ('shoulder_press', 'kettlebell_press_double', 40, false),
  ('shoulder_press', 'kettlebell_push_press', 50, false),
  ('shoulder_press', 'smith_machine_shoulder_press', 60, false),
  ('shoulder_press', 'shoulder_press_machine', 70, false),
  ('shoulder_press', 'shoulder_press_plate_loaded', 80, false),
  ('lateral_raise', 'dumbbell_lateral_raise', 10, true),
  ('lateral_raise', 'lateral_raise_cable', 20, false),
  ('lateral_raise', 'one_arm_cable_lateral_raise', 30, false),
  ('lateral_raise', 'two_arm_cable_lateral_raise', 40, false),
  ('lateral_raise', 'lateral_raise_machine', 50, false),
  ('lateral_raise', 'lateral_raise', 60, false),
  ('lateral_raise', 'seated_lateral_raise', 70, false),
  ('rear_delt', 'cable_reverse_fly', 10, true),
  ('rear_delt', 'rear_delt_machine', 20, false),
  ('rear_delt', 'face_pull', 30, false),
  ('rear_delt', 'band_pull_apart', 40, false),
  ('biceps_curl', 'dumbbell_curl', 10, true),
  ('biceps_curl', 'barbell_curl', 20, false),
  ('biceps_curl', 'cable_curl', 30, false),
  ('biceps_curl', 'incline_dumbbell_curl', 40, false),
  ('biceps_curl', 'bicep_curl_machine', 50, false),
  ('biceps_curl', 'preacher_curl', 60, false),
  ('biceps_curl', 'seated_biceps_plate_loaded', 70, false),
  ('biceps_curl', 'hammer_curl', 80, false),
  ('triceps_extension', 'cable_triceps_pushdown', 10, true),
  ('triceps_extension', 'rope_pushdown', 20, false),
  ('triceps_extension', 'overhead_tricep_rope_extension', 30, false),
  ('triceps_extension', 'triceps_extension', 40, false),
  ('dip', 'dip', 10, true),
  ('dip', 'weighted_dip', 20, false),
  ('dip', 'seated_dip', 30, false),
  ('squat', 'barbell_back_squat', 10, true),
  ('squat', 'kettlebell_goblet_squat', 20, false),
  ('squat', 'squat', 30, false),
  ('squat', 'belt_squat_plate_loaded', 40, false),
  ('squat', 'hack_squat_plate_loaded', 50, false),
  ('squat', 'pendulum_squat_plate_loaded', 60, false),
  ('squat', 'multi_squat_ground_base', 70, false),
  ('squat', 'v_squat_plate_loaded', 80, false),
  ('leg_press', 'leg_press', 10, true),
  ('leg_press', 'super_squat_press_plate_loaded', 20, false),
  ('split_squat_lunge', 'bulgarian_split_squat', 10, true),
  ('deadlift', 'deadlift', 10, true),
  ('deadlift', 'romanian_deadlift', 20, false),
  ('deadlift', 'kettlebell_deadlift', 30, false),
  ('deadlift', 'kettlebell_rdl', 40, false),
  ('deadlift', 'glute_ham_fixed_pad', 50, false),
  ('kettlebell_swing', 'kettlebell_swing_two_hand', 10, true),
  ('kettlebell_swing', 'kettlebell_swing_one_hand', 20, false),
  ('hip_thrust_bridge', 'barbell_hip_thrust', 10, true),
  ('hip_thrust_bridge', 'glute_bridge', 20, false),
  ('hip_thrust_bridge', 'single_leg_glute_bridge', 30, false),
  ('hip_thrust_bridge', 'machine_hip_thrust', 40, false),
  ('hip_thrust_bridge', 'glute_drive_plate_loaded', 50, false),
  ('hip_thrust_bridge', 'hip_and_glute_selectorized', 60, false),
  ('hip_thrust_bridge', 'hip_extension', 70, false),
  ('leg_curl', 'seated_leg_curl', 10, true),
  ('leg_curl', 'lying_leg_curl', 20, false),
  ('leg_curl', 'prone_leg_curl', 30, false),
  ('leg_curl', 'seated_leg_curl_plate_loaded', 40, false),
  ('leg_curl', 'prone_leg_curl_plate_loaded', 50, false),
  ('leg_curl', 'kneeling_leg_curl_plate_loaded', 60, false),
  ('leg_curl', 'assisted_nordic_hamstring_curl', 70, false),
  ('leg_extension', 'leg_extension', 10, true),
  ('calf_raise', 'standing_calf_raise', 10, true),
  ('calf_raise', 'seated_calf_raise', 20, false),
  ('calf_raise', 'seated_calf', 30, false),
  ('hip_abduction', 'hip_abduction', 10, true),
  ('hip_abduction', 'seated_hip_abduction', 20, false),
  ('hip_abduction', 'clamshell', 30, false),
  ('hip_abduction', 'lateral_band_walk', 40, false),
  ('hip_abduction', 'monster_walk', 50, false),
  ('hip_adduction', 'hip_adduction', 10, true),
  ('hip_adduction', 'seated_hip_adduction', 20, false),
  ('core_flexion', 'crunch', 10, true),
  ('core_flexion', 'reverse_crunch', 20, false),
  ('core_flexion', 'sit_up', 30, false),
  ('core_flexion', 'v_up', 40, false),
  ('core_flexion', 'abdominal_crunch_machine', 50, false),
  ('core_flexion', 'ab_isolator', 60, false),
  ('core_flexion', 'kneeling_cable_crunch', 70, false),
  ('core_stability', 'plank', 10, true),
  ('core_stability', 'side_plank', 20, false),
  ('core_stability', 'dead_bug', 30, false),
  ('core_stability', 'bird_dog', 40, false),
  ('core_stability', 'hollow_hold', 50, false),
  ('core_stability', 'mountain_climbers', 60, false),
  ('leg_raise', 'hanging_leg_raise', 10, true),
  ('leg_raise', 'hanging_knee_raise', 20, false),
  ('leg_raise', 'leg_raise_lying', 30, false),
  ('loaded_carry', 'kettlebell_farmer_carry_double', 10, true),
  ('loaded_carry', 'kettlebell_suitcase_carry', 20, false),
  ('loaded_carry', 'kettlebell_rack_carry', 30, false);

DO $$
DECLARE
  missing_pairs text;
BEGIN
  SELECT string_agg(s.family_slug || '/' || s.exercise_slug, ', ' ORDER BY s.family_slug, s.exercise_slug)
  INTO missing_pairs
  FROM exercise_family_member_seed_v1 s
  LEFT JOIN catalog_dev.exercise_family f
    ON f.slug = s.family_slug
  LEFT JOIN catalog_dev.exercise e
    ON e.slug = s.exercise_slug
  WHERE f.exercise_family_id IS NULL
     OR e.exercise_id IS NULL;

  IF missing_pairs IS NOT NULL THEN
    RAISE EXCEPTION 'exercise family seed references missing rows: %', missing_pairs;
  END IF;
END
$$;

INSERT INTO catalog_dev.exercise_family_member (
  exercise_family_id,
  exercise_id,
  variant_label,
  is_default,
  sort_order,
  is_active,
  source
)
SELECT
  f.exercise_family_id,
  e.exercise_id,
  e.display_name,
  s.is_default,
  s.sort_order,
  true,
  'system_seed_v1'
FROM exercise_family_member_seed_v1 s
JOIN catalog_dev.exercise_family f
  ON f.slug = s.family_slug
JOIN catalog_dev.exercise e
  ON e.slug = s.exercise_slug
ON CONFLICT (exercise_family_id, exercise_id) DO UPDATE
SET
  variant_label = EXCLUDED.variant_label,
  is_default = EXCLUDED.is_default,
  sort_order = EXCLUDED.sort_order,
  is_active = true,
  source = EXCLUDED.source;

COMMIT;
