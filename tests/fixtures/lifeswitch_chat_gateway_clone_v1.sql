BEGIN;

CREATE ROLE sage NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE brains_app LOGIN PASSWORD 'clone-only-password' NOSUPERUSER
  NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

CREATE SCHEMA lifeswitch_agentic AUTHORIZATION sage;
CREATE SCHEMA lifeswitch_plan AUTHORIZATION sage;
CREATE SCHEMA lifeswitch_nutrition AUTHORIZATION sage;
CREATE SCHEMA lifeswitch_training AUTHORIZATION sage;
GRANT CREATE ON SCHEMA public TO sage;

SET ROLE sage;

CREATE TABLE lifeswitch_agentic.plan_versions (
  id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  owner_timezone text NOT NULL,
  document jsonb NOT NULL
);
CREATE TABLE lifeswitch_agentic.plan_owner_state (
  owner_user_id uuid PRIMARY KEY,
  active_plan_version_id uuid NOT NULL
);

CREATE TABLE lifeswitch_plan.plan_profile (
  plan_profile_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  phase text NOT NULL,
  phase_label text NOT NULL,
  primary_goal text NOT NULL,
  start_date date,
  review_date date,
  review_cadence text NOT NULL,
  body_state jsonb NOT NULL DEFAULT '{}'::jsonb,
  nutrition_targets jsonb NOT NULL DEFAULT '{}'::jsonb,
  training_targets jsonb NOT NULL DEFAULT '{}'::jsonb,
  conditioning_targets jsonb NOT NULL DEFAULT '{}'::jsonb,
  activity_targets jsonb NOT NULL DEFAULT '{}'::jsonb,
  recovery_targets jsonb NOT NULL DEFAULT '{}'::jsonb,
  monitoring_rules jsonb NOT NULL DEFAULT '{}'::jsonb,
  coach_notes text NOT NULL DEFAULT '',
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);

CREATE TABLE lifeswitch_nutrition.nutrition_day (
  nutrition_day_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  day date NOT NULL
);
CREATE TABLE lifeswitch_nutrition.my_food (
  my_food_id uuid PRIMARY KEY,
  kcal numeric NOT NULL,
  protein_g numeric NOT NULL,
  carbs_g numeric NOT NULL,
  fat_g numeric NOT NULL
);
CREATE TABLE lifeswitch_nutrition.my_food_serving (
  my_food_serving_id uuid PRIMARY KEY,
  my_food_id uuid NOT NULL,
  grams numeric NOT NULL
);
CREATE TABLE lifeswitch_nutrition.nutrition_entry (
  nutrition_entry_id uuid PRIMARY KEY,
  nutrition_day_id uuid NOT NULL,
  my_food_id uuid,
  my_food_serving_id uuid,
  meal_id uuid,
  qty_g numeric,
  qty_servings numeric
);
CREATE TABLE lifeswitch_nutrition.meal_item (
  meal_item_id uuid PRIMARY KEY,
  meal_id uuid NOT NULL,
  my_food_id uuid NOT NULL,
  my_food_serving_id uuid,
  qty_g numeric,
  qty_servings numeric
);

CREATE TABLE lifeswitch_training.training_session_current_v (
  training_session_id uuid PRIMARY KEY,
  owner_user_id uuid,
  day date,
  name text,
  finished_at timestamptz
);
CREATE TABLE lifeswitch_training.training_set_log (
  training_set_log_id uuid PRIMARY KEY,
  training_session_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  exercise_id text NOT NULL,
  exercise_name text NOT NULL,
  weight numeric NOT NULL,
  reps integer NOT NULL,
  volume numeric NOT NULL,
  is_active boolean NOT NULL,
  capture_role text NOT NULL,
  load_unit text NOT NULL
);
CREATE TABLE lifeswitch_training.conditioning_session_current_v (
  conditioning_session_log_id uuid PRIMARY KEY,
  owner_user_id uuid,
  day date,
  name text,
  category text,
  modality text,
  duration_min numeric,
  intensity text,
  distance_value numeric,
  distance_unit text,
  heart_rate_avg numeric,
  recovery_impact text,
  is_active boolean,
  created_at timestamptz
);

CREATE TABLE public.lifeswitch_measurement_entries (
  measurement_entry_id uuid PRIMARY KEY,
  owner_user_id text NOT NULL,
  local_date date NOT NULL,
  weight_value numeric,
  weight_unit text NOT NULL,
  waist_value numeric,
  body_fat_percent numeric,
  measurement_unit text NOT NULL,
  is_active boolean NOT NULL
);

RESET ROLE;

INSERT INTO lifeswitch_agentic.plan_versions(
  id,owner_user_id,owner_timezone,document
) VALUES
(
  'aaaaaaaa-0000-4000-8000-000000000001',
  '11111111-1111-4111-8111-111111111111',
  'America/Chicago',
  '{"schema_version":1,"phase":"lean_gain","phase_label":"Build","primary_goal":"Owner A goal","coach_notes":"owner-a-secret","body_state":{"private":"a"},"monitoring_rules":{"private":"a"},"nutrition_targets":{"calorie_target":{"nominal_kcal":2800,"private_note":"owner-a-private"},"protein_target":{"minimum_g":190,"private_note":"owner-a-private"},"macro_notes":"owner-a-private"},"training_targets":{"workouts_per_week":4}}'
),
(
  'bbbbbbbb-0000-4000-8000-000000000001',
  '22222222-2222-4222-8222-222222222222',
  'America/Los_Angeles',
  '{"schema_version":1,"phase":"maintenance","phase_label":"Maintain","primary_goal":"Owner B goal","coach_notes":"owner-b-secret","nutrition_targets":{"calorie_target":2100,"protein_g":140}}'
);
INSERT INTO lifeswitch_agentic.plan_owner_state(
  owner_user_id,active_plan_version_id
) VALUES
('11111111-1111-4111-8111-111111111111','aaaaaaaa-0000-4000-8000-000000000001'),
('22222222-2222-4222-8222-222222222222','bbbbbbbb-0000-4000-8000-000000000001');

INSERT INTO lifeswitch_nutrition.nutrition_day(
  nutrition_day_id,owner_user_id,day
) VALUES
('aaaaaaaa-1000-4000-8000-000000000001','11111111-1111-4111-8111-111111111111','2026-07-29'),
('bbbbbbbb-1000-4000-8000-000000000001','22222222-2222-4222-8222-222222222222','2026-07-29');
INSERT INTO lifeswitch_nutrition.my_food(
  my_food_id,kcal,protein_g,carbs_g,fat_g
) VALUES
('aaaaaaaa-2000-4000-8000-000000000001',100,10,12,2),
('bbbbbbbb-2000-4000-8000-000000000001',900,90,100,30);
INSERT INTO lifeswitch_nutrition.nutrition_entry(
  nutrition_entry_id,nutrition_day_id,my_food_id,qty_g
) VALUES
('aaaaaaaa-3000-4000-8000-000000000001','aaaaaaaa-1000-4000-8000-000000000001','aaaaaaaa-2000-4000-8000-000000000001',100),
('bbbbbbbb-3000-4000-8000-000000000001','bbbbbbbb-1000-4000-8000-000000000001','bbbbbbbb-2000-4000-8000-000000000001',100);

INSERT INTO lifeswitch_training.training_session_current_v(
  training_session_id,owner_user_id,day,name,finished_at
) VALUES
('aaaaaaaa-4000-4000-8000-000000000001','11111111-1111-4111-8111-111111111111','2026-07-29','Owner A workout',pg_catalog.clock_timestamp()),
('bbbbbbbb-4000-4000-8000-000000000001','22222222-2222-4222-8222-222222222222','2026-07-29','Owner B workout',pg_catalog.clock_timestamp());
INSERT INTO lifeswitch_training.training_set_log(
  training_set_log_id,training_session_id,owner_user_id,exercise_id,
  exercise_name,weight,reps,volume,is_active,capture_role,load_unit
) VALUES
('aaaaaaaa-5000-4000-8000-000000000001','aaaaaaaa-4000-4000-8000-000000000001','11111111-1111-4111-8111-111111111111','squat','Back Squat',200,5,1000,true,'strength','lb'),
('bbbbbbbb-5000-4000-8000-000000000001','bbbbbbbb-4000-4000-8000-000000000001','22222222-2222-4222-8222-222222222222','squat','Back Squat',400,5,2000,true,'strength','lb');
INSERT INTO lifeswitch_training.conditioning_session_current_v(
  conditioning_session_log_id,owner_user_id,day,name,category,modality,
  duration_min,intensity,distance_value,distance_unit,heart_rate_avg,
  recovery_impact,is_active,created_at
) VALUES
('aaaaaaaa-6000-4000-8000-000000000001','11111111-1111-4111-8111-111111111111','2026-07-29','Owner A walk','cardio','walk',20,'easy',1,'mi',110,'low',true,pg_catalog.clock_timestamp()),
('bbbbbbbb-6000-4000-8000-000000000001','22222222-2222-4222-8222-222222222222','2026-07-29','Owner B run','cardio','run',60,'hard',6,'mi',165,'high',true,pg_catalog.clock_timestamp());
INSERT INTO public.lifeswitch_measurement_entries(
  measurement_entry_id,owner_user_id,local_date,weight_value,weight_unit,
  waist_value,body_fat_percent,measurement_unit,is_active
) VALUES
('aaaaaaaa-7000-4000-8000-000000000001','11111111-1111-4111-8111-111111111111','2026-07-29',200,'lb',35,15,'in',true),
('bbbbbbbb-7000-4000-8000-000000000001','22222222-2222-4222-8222-222222222222','2026-07-29',300,'lb',45,25,'in',true);

COMMIT;
