BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lifeswitch_chat_reader_v1') THEN
    CREATE ROLE lifeswitch_chat_reader_v1 NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lifeswitch_chat_binding_writer_v1') THEN
    CREATE ROLE lifeswitch_chat_binding_writer_v1 NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS lifeswitch_chat AUTHORIZATION sage;
REVOKE ALL ON SCHEMA lifeswitch_chat FROM PUBLIC;
GRANT USAGE ON SCHEMA lifeswitch_chat
  TO lifeswitch_chat_reader_v1,lifeswitch_chat_binding_writer_v1;

CREATE TABLE lifeswitch_chat.account_timezone_v1 (
  owner_user_id uuid PRIMARY KEY,
  timezone_name text NOT NULL,
  source text NOT NULL DEFAULT 'account_setting'
    CHECK (source IN ('account_setting','reviewed_migration')),
  revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (char_length(timezone_name) BETWEEN 1 AND 80)
);
INSERT INTO lifeswitch_chat.account_timezone_v1(
  owner_user_id,timezone_name,source
)
SELECT state.owner_user_id,version.owner_timezone,'reviewed_migration'
FROM lifeswitch_agentic.plan_owner_state state
JOIN lifeswitch_agentic.plan_versions version
  ON version.owner_user_id=state.owner_user_id
 AND version.id=state.active_plan_version_id
JOIN pg_catalog.pg_timezone_names timezone_catalog
  ON timezone_catalog.name=version.owner_timezone
ON CONFLICT (owner_user_id) DO NOTHING;
ALTER TABLE lifeswitch_chat.account_timezone_v1 OWNER TO sage;
ALTER TABLE lifeswitch_chat.account_timezone_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_chat.account_timezone_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY account_timezone_reader_v1
  ON lifeswitch_chat.account_timezone_v1
  FOR SELECT TO lifeswitch_chat_reader_v1
  USING (
    owner_user_id=NULLIF(current_setting('app.lifeswitch_owner_id',true),'')::uuid
  );
REVOKE ALL ON lifeswitch_chat.account_timezone_v1 FROM PUBLIC,brains_app;
GRANT SELECT ON lifeswitch_chat.account_timezone_v1 TO lifeswitch_chat_reader_v1;

/* Column-level grants omit free-form and raw-capture fields. */
GRANT USAGE ON SCHEMA lifeswitch_agentic,lifeswitch_plan,
  lifeswitch_nutrition,lifeswitch_training,public
  TO lifeswitch_chat_reader_v1;
GRANT SELECT (owner_user_id,active_plan_version_id)
  ON lifeswitch_agentic.plan_owner_state TO lifeswitch_chat_reader_v1;
GRANT SELECT (id,owner_user_id,owner_timezone,document)
  ON lifeswitch_agentic.plan_versions TO lifeswitch_chat_reader_v1;
GRANT SELECT (
  owner_user_id,phase,phase_label,primary_goal,start_date,review_date,
  review_cadence,body_state,nutrition_targets,training_targets,
  conditioning_targets,activity_targets,recovery_targets,monitoring_rules,is_active
) ON lifeswitch_plan.plan_profile TO lifeswitch_chat_reader_v1;
GRANT SELECT (nutrition_day_id,owner_user_id,day)
  ON lifeswitch_nutrition.nutrition_day TO lifeswitch_chat_reader_v1;
GRANT SELECT (
  nutrition_entry_id,nutrition_day_id,meal_id,my_food_id,qty_g,
  my_food_serving_id,qty_servings
) ON lifeswitch_nutrition.nutrition_entry TO lifeswitch_chat_reader_v1;
GRANT SELECT (my_food_id,kcal,protein_g,carbs_g,fat_g)
  ON lifeswitch_nutrition.my_food TO lifeswitch_chat_reader_v1;
GRANT SELECT (my_food_serving_id,my_food_id,grams)
  ON lifeswitch_nutrition.my_food_serving TO lifeswitch_chat_reader_v1;
GRANT SELECT (meal_id,my_food_id,qty_g,my_food_serving_id,qty_servings)
  ON lifeswitch_nutrition.meal_item TO lifeswitch_chat_reader_v1;
GRANT SELECT (
  training_session_id,owner_user_id,day,name,finished_at,created_at
) ON lifeswitch_training.training_session_current_v TO lifeswitch_chat_reader_v1;
GRANT SELECT (
  training_set_log_id,training_session_id,owner_user_id,exercise_id,
  exercise_name,weight,reps,volume,is_active,capture_role,load_unit
) ON lifeswitch_training.training_set_log TO lifeswitch_chat_reader_v1;
GRANT SELECT (
  conditioning_session_log_id,owner_user_id,day,name,category,modality,
  duration_min,intensity,distance_value,distance_unit,heart_rate_avg,
  recovery_impact,is_active,created_at
) ON lifeswitch_training.conditioning_session_current_v
  TO lifeswitch_chat_reader_v1;
GRANT SELECT (
  owner_user_id,local_date,weight_value,weight_unit,waist_value,
  body_fat_percent,measurement_unit,is_active
) ON public.lifeswitch_measurement_entries TO lifeswitch_chat_reader_v1;

CREATE TABLE lifeswitch_chat.final_answer_lifeswitch_binding_v1 (
  answer_id uuid PRIMARY KEY,
  authenticated_actor_user_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  request_id_sha256 text NOT NULL CHECK (request_id_sha256 ~ '^[0-9a-f]{64}$'),
  conversation_snapshot_sha256 text NOT NULL
    CHECK (conversation_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
  source_assembly_sha256 text NOT NULL
    CHECK (source_assembly_sha256 ~ '^[0-9a-f]{64}$'),
  envelope_sha256 text NOT NULL CHECK (envelope_sha256 ~ '^[0-9a-f]{64}$'),
  rendered_content_sha256 text NOT NULL
    CHECK (rendered_content_sha256 ~ '^[0-9a-f]{64}$'),
  answer_model_exposed boolean NOT NULL CHECK (answer_model_exposed),
  record_count integer NOT NULL CHECK (record_count BETWEEN 0 AND 500),
  rendered_tokens integer NOT NULL CHECK (rendered_tokens BETWEEN 0 AND 1000),
  record_refs jsonb NOT NULL CHECK (jsonb_typeof(record_refs)='array'),
  created_at timestamptz NOT NULL,
  persisted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  binding_manifest_sha256 text NOT NULL UNIQUE
    CHECK (binding_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (authenticated_actor_user_id=owner_user_id)
);
ALTER TABLE lifeswitch_chat.final_answer_lifeswitch_binding_v1 OWNER TO sage;
ALTER TABLE lifeswitch_chat.final_answer_lifeswitch_binding_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_chat.final_answer_lifeswitch_binding_v1
  FORCE ROW LEVEL SECURITY;
CREATE POLICY final_answer_lifeswitch_binding_writer_v1
  ON lifeswitch_chat.final_answer_lifeswitch_binding_v1
  FOR ALL TO lifeswitch_chat_binding_writer_v1
  USING (
    owner_user_id=NULLIF(current_setting('app.lifeswitch_owner_id',true),'')::uuid
  )
  WITH CHECK (
    owner_user_id=NULLIF(current_setting('app.lifeswitch_owner_id',true),'')::uuid
  );
REVOKE ALL ON lifeswitch_chat.final_answer_lifeswitch_binding_v1
  FROM PUBLIC,brains_app;
GRANT SELECT,INSERT ON lifeswitch_chat.final_answer_lifeswitch_binding_v1
  TO lifeswitch_chat_binding_writer_v1;

CREATE FUNCTION lifeswitch_chat.reject_binding_mutation_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path=pg_catalog
AS $$
BEGIN
  RAISE EXCEPTION 'LifeSwitch answer bindings are append-only';
END
$$;
ALTER FUNCTION lifeswitch_chat.reject_binding_mutation_v1() OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.reject_binding_mutation_v1() FROM PUBLIC;
CREATE TRIGGER final_answer_lifeswitch_binding_immutable_v1
BEFORE UPDATE OR DELETE
ON lifeswitch_chat.final_answer_lifeswitch_binding_v1
FOR EACH ROW EXECUTE FUNCTION lifeswitch_chat.reject_binding_mutation_v1();

GRANT lifeswitch_chat_reader_v1,lifeswitch_chat_binding_writer_v1
  TO brains_app WITH INHERIT FALSE,SET TRUE;

COMMENT ON SCHEMA lifeswitch_chat IS
  'Private owner-scoped LifeSwitch chat context and answer-audit boundary.';
COMMENT ON TABLE lifeswitch_chat.account_timezone_v1 IS
  'Backend-authoritative account timezone; active-plan timezone is a bounded fallback.';
COMMENT ON TABLE lifeswitch_chat.final_answer_lifeswitch_binding_v1 IS
  'Append-only answer binding for structured LifeSwitch context; never Memory evidence.';

COMMIT;
