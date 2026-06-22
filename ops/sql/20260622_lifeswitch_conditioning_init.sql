BEGIN;

CREATE TABLE IF NOT EXISTS lifeswitch_training.conditioning_library (
  conditioning_library_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  slug                    text NOT NULL UNIQUE,
  name                    text NOT NULL,
  category                text NOT NULL,
  modality                text NOT NULL,

  purpose                 text NOT NULL DEFAULT '',
  default_duration_min    int NOT NULL DEFAULT 0,
  default_frequency_per_week numeric NOT NULL DEFAULT 0,
  default_intensity       text NOT NULL DEFAULT '',

  interference_risk       text NOT NULL DEFAULT '',
  joint_stress            text NOT NULL DEFAULT '',
  equipment               text NOT NULL DEFAULT '',

  progression_notes       text NOT NULL DEFAULT '',
  contraindication_notes  text NOT NULL DEFAULT '',

  sort_order              int NOT NULL DEFAULT 100,
  is_active               boolean NOT NULL DEFAULT true,

  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_conditioning_library_updated_at ON lifeswitch_training.conditioning_library;
CREATE TRIGGER trg_conditioning_library_updated_at
BEFORE UPDATE ON lifeswitch_training.conditioning_library
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_conditioning_library_active_sort
  ON lifeswitch_training.conditioning_library(is_active, sort_order, lower(name));


CREATE TABLE IF NOT EXISTS lifeswitch_training.my_conditioning_prescription (
  my_conditioning_prescription_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id                   uuid NOT NULL,

  conditioning_library_id          uuid REFERENCES lifeswitch_training.conditioning_library(conditioning_library_id),

  name                             text NOT NULL,
  category                         text NOT NULL DEFAULT '',
  modality                         text NOT NULL DEFAULT '',

  purpose                          text NOT NULL DEFAULT '',
  target_duration_min              int NOT NULL DEFAULT 0,
  target_frequency_per_week        numeric NOT NULL DEFAULT 0,
  target_intensity                 text NOT NULL DEFAULT '',

  preferred_timing                 text NOT NULL DEFAULT '',
  recovery_constraints             text NOT NULL DEFAULT '',
  notes                            text NOT NULL DEFAULT '',

  is_active                        boolean NOT NULL DEFAULT true,

  created_at                       timestamptz NOT NULL DEFAULT now(),
  updated_at                       timestamptz NOT NULL DEFAULT now(),

  UNIQUE(owner_user_id, name)
);

DROP TRIGGER IF EXISTS trg_my_conditioning_prescription_updated_at ON lifeswitch_training.my_conditioning_prescription;
CREATE TRIGGER trg_my_conditioning_prescription_updated_at
BEFORE UPDATE ON lifeswitch_training.my_conditioning_prescription
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.tg_set_updated_at();

CREATE INDEX IF NOT EXISTS ix_my_conditioning_owner
  ON lifeswitch_training.my_conditioning_prescription(owner_user_id);

CREATE INDEX IF NOT EXISTS ix_my_conditioning_owner_active
  ON lifeswitch_training.my_conditioning_prescription(owner_user_id, is_active, updated_at DESC);


INSERT INTO lifeswitch_training.conditioning_library
  (slug, name, category, modality, purpose, default_duration_min, default_frequency_per_week,
   default_intensity, interference_risk, joint_stress, equipment, progression_notes,
   contraindication_notes, sort_order, is_active)
VALUES
  (
    'outdoor_walk',
    'Outdoor Walk',
    'walking_neat',
    'walking',
    'Low-stress activity, general health, energy expenditure, and recovery support.',
    30,
    3,
    'Easy conversational pace.',
    'low',
    'low',
    'none',
    'Increase duration first, then frequency, then pace.',
    'Watch foot, ankle, knee, hip, or low-back tolerance.',
    10,
    true
  ),
  (
    'post_meal_walk',
    'Post-Meal Walk',
    'walking_neat',
    'walking',
    'Short walk after meals for glucose management, digestion, and daily activity.',
    10,
    7,
    'Very easy to easy.',
    'very_low',
    'low',
    'none',
    'Add after one meal first, then expand to more meals if useful.',
    'Keep easy; this is not intended to compete with training.',
    20,
    true
  ),
  (
    'daily_step_target',
    'Daily Step Target',
    'walking_neat',
    'daily_activity',
    'Daily movement target for NEAT, body-composition support, and health.',
    0,
    7,
    'Distributed across the day.',
    'very_low',
    'low',
    'phone or wearable',
    'Raise target gradually; avoid large sudden jumps.',
    'Watch foot/calf tolerance and avoid chasing steps at the expense of recovery.',
    30,
    true
  ),
  (
    'incline_treadmill_walk',
    'Incline Treadmill Walk',
    'low_intensity_cardio',
    'treadmill',
    'Fat-loss support and aerobic base with relatively low skill demand.',
    25,
    2,
    'Easy to moderate; conversational pace.',
    'low_to_moderate',
    'moderate',
    'treadmill',
    'Increase duration first, then frequency, then incline/speed.',
    'Watch calves, feet, Achilles, knees, and leg recovery.',
    40,
    true
  ),
  (
    'easy_treadmill_walk',
    'Easy Treadmill Walk',
    'low_intensity_cardio',
    'treadmill',
    'Controlled indoor walking for activity and low-stress conditioning.',
    30,
    2,
    'Easy conversational pace.',
    'low',
    'low_to_moderate',
    'treadmill',
    'Increase duration or frequency gradually.',
    'Keep easy if legs are sore or lower-body training is high.',
    50,
    true
  ),
  (
    'zone2_stationary_bike',
    'Zone 2 Stationary Bike',
    'zone2_aerobic',
    'bike',
    'Aerobic base with low joint impact and low eccentric load.',
    30,
    2,
    'Conversational, steady, sustainable effort.',
    'low',
    'low',
    'stationary bike',
    'Increase duration before resistance or frequency.',
    'Avoid turning it into intervals unless intended.',
    60,
    true
  ),
  (
    'zone2_elliptical',
    'Zone 2 Elliptical',
    'zone2_aerobic',
    'elliptical',
    'Aerobic base with low impact and whole-body rhythm.',
    30,
    2,
    'Conversational, steady, sustainable effort.',
    'low_to_moderate',
    'low_to_moderate',
    'elliptical',
    'Increase duration before intensity.',
    'Watch hip flexor, knee, and low-back tolerance.',
    70,
    true
  ),
  (
    'zone2_rower',
    'Zone 2 Rower',
    'zone2_aerobic',
    'rower',
    'Aerobic base with more posterior-chain and technical demand.',
    20,
    2,
    'Steady moderate rhythm; avoid sprinting.',
    'moderate',
    'moderate',
    'rower',
    'Increase duration gradually and preserve technique.',
    'Can interfere with back, hamstrings, grip, or leg recovery if overdone.',
    80,
    true
  ),
  (
    'zone2_stair_climber',
    'Zone 2 Stair Climber',
    'zone2_aerobic',
    'stair_climber',
    'Aerobic conditioning with higher lower-body demand.',
    20,
    1,
    'Easy to moderate; sustainable.',
    'moderate_to_high',
    'moderate_to_high',
    'stair climber',
    'Progress slowly; increase duration before intensity.',
    'May interfere with leg training or irritate knees/calves/feet.',
    90,
    true
  ),
  (
    'light_recovery_bike',
    'Light Recovery Bike',
    'recovery',
    'bike',
    'Low-stress circulation and recovery support.',
    15,
    2,
    'Very easy.',
    'very_low',
    'very_low',
    'stationary bike',
    'Keep intensity very low; extend only if recovery improves.',
    'Should not create fatigue.',
    100,
    true
  ),
  (
    'bike_intervals',
    'Bike Intervals',
    'intervals',
    'bike',
    'Higher-intensity conditioning with relatively low joint impact.',
    20,
    1,
    'Hard intervals with easy recovery periods.',
    'moderate_to_high',
    'low_to_moderate',
    'stationary bike',
    'Start with few intervals; add volume slowly.',
    'Higher fatigue cost; avoid if recovery or leg performance is compromised.',
    110,
    true
  ),
  (
    'rower_intervals',
    'Rower Intervals',
    'intervals',
    'rower',
    'High-output conditioning with full-body demand.',
    15,
    1,
    'Hard intervals with full recovery.',
    'high',
    'moderate_to_high',
    'rower',
    'Start conservatively and maintain technique.',
    'Can interfere with back, grip, posterior chain, and leg recovery.',
    120,
    true
  ),
  (
    'hill_walk_intervals',
    'Hill Walk Intervals',
    'intervals',
    'walking',
    'Walking-based intervals using grade or terrain.',
    20,
    1,
    'Moderate uphill efforts with easy walking recovery.',
    'moderate',
    'moderate',
    'hill or treadmill',
    'Increase number of intervals slowly.',
    'Watch calves, Achilles, feet, and knees.',
    130,
    true
  ),
  (
    'sled_push_conditioning',
    'Sled Push Conditioning',
    'conditioning',
    'sled',
    'Conditioning with concentric-biased lower-body work.',
    15,
    1,
    'Moderate to hard pushes with rest.',
    'moderate_to_high',
    'moderate',
    'sled',
    'Start with short pushes and full rest.',
    'Can compete with leg training; watch knees, hips, calves, and low back.',
    140,
    true
  ),
  (
    'farmer_carry_conditioning',
    'Farmer Carry Conditioning',
    'conditioning',
    'loaded_carry',
    'Loaded carry conditioning, grip, trunk, posture, and work capacity.',
    15,
    1,
    'Moderate carries with controlled breathing.',
    'moderate',
    'low_to_moderate',
    'dumbbells, kettlebells, or handles',
    'Increase distance or rounds before load.',
    'Can interfere with grip, traps, low back, or recovery if too heavy.',
    150,
    true
  )
ON CONFLICT (slug) DO UPDATE
SET name=excluded.name,
    category=excluded.category,
    modality=excluded.modality,
    purpose=excluded.purpose,
    default_duration_min=excluded.default_duration_min,
    default_frequency_per_week=excluded.default_frequency_per_week,
    default_intensity=excluded.default_intensity,
    interference_risk=excluded.interference_risk,
    joint_stress=excluded.joint_stress,
    equipment=excluded.equipment,
    progression_notes=excluded.progression_notes,
    contraindication_notes=excluded.contraindication_notes,
    sort_order=excluded.sort_order,
    is_active=excluded.is_active,
    updated_at=now();

COMMIT;
