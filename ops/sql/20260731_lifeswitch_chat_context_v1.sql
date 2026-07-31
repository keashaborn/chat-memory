BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='lifeswitch_chat_reader_v1') THEN
    CREATE ROLE lifeswitch_chat_reader_v1 NOLOGIN NOINHERIT NOSUPERUSER
      NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='lifeswitch_chat_binding_writer_v1') THEN
    CREATE ROLE lifeswitch_chat_binding_writer_v1 NOLOGIN NOINHERIT NOSUPERUSER
      NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS lifeswitch_chat AUTHORIZATION sage;
REVOKE ALL ON SCHEMA lifeswitch_chat FROM PUBLIC;
GRANT USAGE ON SCHEMA lifeswitch_chat
  TO brains_app,lifeswitch_chat_reader_v1,lifeswitch_chat_binding_writer_v1;

/*
 * The reader receives no source-table or source-schema privileges. A trusted
 * server transaction first creates a short-lived, backend-PID-bound context.
 * Read gateways accept only that opaque context ID and derive the owner inside
 * SECURITY DEFINER code. They never accept a caller-selected owner UUID.
 */
CREATE UNLOGGED TABLE lifeswitch_chat.owner_read_context_v1 (
  context_id uuid PRIMARY KEY,
  backend_pid integer NOT NULL,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  request_id_sha256 text NOT NULL CHECK (request_id_sha256 ~ '^[0-9a-f]{64}$'),
  conversation_snapshot_sha256 text NOT NULL
    CHECK (conversation_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  expires_at timestamptz NOT NULL,
  CHECK (expires_at > created_at)
);
ALTER TABLE lifeswitch_chat.owner_read_context_v1 OWNER TO sage;
REVOKE ALL ON lifeswitch_chat.owner_read_context_v1 FROM PUBLIC,brains_app,
  lifeswitch_chat_reader_v1,lifeswitch_chat_binding_writer_v1;
CREATE INDEX owner_read_context_expiry_v1
  ON lifeswitch_chat.owner_read_context_v1(expires_at);

CREATE FUNCTION lifeswitch_chat.begin_owner_read_context_v1(
  p_owner_user_id uuid,
  p_thread_id uuid,
  p_request_id_sha256 text,
  p_conversation_snapshot_sha256 text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $$
DECLARE
  v_authenticated_owner uuid;
  v_lifeswitch_owner uuid;
  v_context_id uuid;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'trusted backend session required' USING ERRCODE='42501';
  END IF;
  v_authenticated_owner := NULLIF(
    pg_catalog.current_setting('app.user_id',true),''
  )::uuid;
  v_lifeswitch_owner := NULLIF(
    pg_catalog.current_setting('app.lifeswitch_owner_id',true),''
  )::uuid;
  IF v_authenticated_owner IS NULL
     OR v_authenticated_owner <> p_owner_user_id
     OR v_lifeswitch_owner <> p_owner_user_id THEN
    RAISE EXCEPTION 'authenticated owner mismatch' USING ERRCODE='42501';
  END IF;
  IF p_request_id_sha256 !~ '^[0-9a-f]{64}$'
     OR p_conversation_snapshot_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid request binding' USING ERRCODE='22023';
  END IF;
  DELETE FROM lifeswitch_chat.owner_read_context_v1
  WHERE expires_at <= pg_catalog.clock_timestamp();
  v_context_id := pg_catalog.gen_random_uuid();
  INSERT INTO lifeswitch_chat.owner_read_context_v1(
    context_id,backend_pid,owner_user_id,thread_id,
    request_id_sha256,conversation_snapshot_sha256,expires_at
  ) VALUES (
    v_context_id,pg_catalog.pg_backend_pid(),p_owner_user_id,p_thread_id,
    p_request_id_sha256,p_conversation_snapshot_sha256,
    pg_catalog.clock_timestamp()+interval '5 minutes'
  );
  RETURN v_context_id;
END
$$;
ALTER FUNCTION lifeswitch_chat.begin_owner_read_context_v1(uuid,uuid,text,text)
  OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.begin_owner_read_context_v1(uuid,uuid,text,text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lifeswitch_chat.begin_owner_read_context_v1(uuid,uuid,text,text)
  TO brains_app;

CREATE FUNCTION lifeswitch_chat.end_owner_read_context_v1(p_context_id uuid)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $$
DECLARE
  v_deleted integer;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'trusted backend session required' USING ERRCODE='42501';
  END IF;
  DELETE FROM lifeswitch_chat.owner_read_context_v1
  WHERE context_id=p_context_id
    AND backend_pid=pg_catalog.pg_backend_pid();
  GET DIAGNOSTICS v_deleted = ROW_COUNT;
  RETURN v_deleted=1;
END
$$;
ALTER FUNCTION lifeswitch_chat.end_owner_read_context_v1(uuid) OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.end_owner_read_context_v1(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lifeswitch_chat.end_owner_read_context_v1(uuid)
  TO brains_app;

CREATE FUNCTION lifeswitch_chat.resolve_owner_read_context_v1(p_context_id uuid)
RETURNS uuid
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
DECLARE
  v_owner_user_id uuid;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'trusted backend session required' USING ERRCODE='42501';
  END IF;
  SELECT owner_user_id INTO v_owner_user_id
  FROM lifeswitch_chat.owner_read_context_v1
  WHERE context_id=p_context_id
    AND backend_pid=pg_catalog.pg_backend_pid()
    AND expires_at > pg_catalog.clock_timestamp();
  IF v_owner_user_id IS NULL THEN
    RAISE EXCEPTION 'owner read context unavailable' USING ERRCODE='42501';
  END IF;
  RETURN v_owner_user_id;
END
$$;
ALTER FUNCTION lifeswitch_chat.resolve_owner_read_context_v1(uuid) OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.resolve_owner_read_context_v1(uuid)
  FROM PUBLIC,brains_app,lifeswitch_chat_reader_v1;

CREATE FUNCTION lifeswitch_chat.whitelist_target_value_v1(p_value jsonb)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path=''
AS $$
DECLARE
  v_kind text;
  v_result jsonb;
BEGIN
  IF p_value IS NULL THEN
    RETURN NULL;
  END IF;
  v_kind := pg_catalog.jsonb_typeof(p_value);
  IF v_kind IN ('number','string','boolean') THEN
    RETURN p_value;
  END IF;
  IF v_kind <> 'object' THEN
    RETURN NULL;
  END IF;
  SELECT pg_catalog.jsonb_object_agg(item.key,filtered.value)
  INTO v_result
  FROM pg_catalog.jsonb_each(p_value) AS item
  CROSS JOIN LATERAL (
    SELECT lifeswitch_chat.whitelist_target_value_v1(item.value) AS value
  ) AS filtered
  WHERE item.key=ANY(ARRAY[
    'lower','upper','minimum','maximum','min','max','value','target',
    'nominal_kcal','daily_range_kcal','rolling_average_kcal','window_days',
    'minimum_g','weekly_adherence','mode','required_hit_days'
  ])
    AND filtered.value IS NOT NULL;
  RETURN NULLIF(COALESCE(v_result,'{}'::jsonb),'{}'::jsonb);
END
$$;
ALTER FUNCTION lifeswitch_chat.whitelist_target_value_v1(jsonb) OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.whitelist_target_value_v1(jsonb)
  FROM PUBLIC,brains_app,lifeswitch_chat_reader_v1;

CREATE FUNCTION lifeswitch_chat.whitelist_target_section_v1(
  p_value jsonb,p_keys text[]
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path=''
AS $$
  SELECT COALESCE(pg_catalog.jsonb_object_agg(item.key,filtered.value),'{}'::jsonb)
  FROM pg_catalog.jsonb_each(COALESCE(p_value,'{}'::jsonb)) AS item
  CROSS JOIN LATERAL (
    SELECT lifeswitch_chat.whitelist_target_value_v1(item.value) AS value
  ) AS filtered
  WHERE item.key=ANY(p_keys) AND filtered.value IS NOT NULL
$$;
ALTER FUNCTION lifeswitch_chat.whitelist_target_section_v1(jsonb,text[])
  OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.whitelist_target_section_v1(jsonb,text[])
  FROM PUBLIC,brains_app,lifeswitch_chat_reader_v1;

CREATE FUNCTION lifeswitch_chat.whitelist_plan_document_v1(p_document jsonb)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path=''
AS $$
  SELECT pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
    'schema_version',1,
    'phase',p_document->'phase',
    'phase_label',p_document->'phase_label',
    'primary_goal',p_document->'primary_goal',
    'start_date',p_document->'start_date',
    'review_date',p_document->'review_date',
    'review_cadence',p_document->'review_cadence',
    'nutrition_targets',NULLIF(lifeswitch_chat.whitelist_target_section_v1(
      p_document->'nutrition_targets',ARRAY[
        'calories','target_kcal','kcal','calorie_target','calorie_range',
        'protein_g','target_protein_g','protein','protein_target',
        'protein_grams_minimum','protein_minimum_g','carbs_g','fat_g'
      ]
    ),'{}'::jsonb),
    'training_targets',NULLIF(lifeswitch_chat.whitelist_target_section_v1(
      p_document->'training_targets',ARRAY[
        'workouts_per_week','strength_sessions_per_week','sessions_per_week'
      ]
    ),'{}'::jsonb),
    'conditioning_targets',NULLIF(lifeswitch_chat.whitelist_target_section_v1(
      p_document->'conditioning_targets',ARRAY[
        'sessions_per_week','minutes_per_week','duration_min'
      ]
    ),'{}'::jsonb),
    'activity_targets',NULLIF(lifeswitch_chat.whitelist_target_section_v1(
      p_document->'activity_targets',ARRAY['steps','steps_per_day']
    ),'{}'::jsonb),
    'recovery_targets',NULLIF(lifeswitch_chat.whitelist_target_section_v1(
      p_document->'recovery_targets',ARRAY['sleep_hours','rest_days']
    ),'{}'::jsonb)
  ))
$$;
ALTER FUNCTION lifeswitch_chat.whitelist_plan_document_v1(jsonb) OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.whitelist_plan_document_v1(jsonb)
  FROM PUBLIC,brains_app,lifeswitch_chat_reader_v1;

CREATE TABLE lifeswitch_chat.account_timezone_v1 (
  owner_user_id uuid PRIMARY KEY,
  timezone_name text NOT NULL,
  source text NOT NULL DEFAULT 'account_setting'
    CHECK (source IN ('account_setting','reviewed_migration')),
  revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CHECK (pg_catalog.char_length(timezone_name) BETWEEN 1 AND 80)
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
REVOKE ALL ON lifeswitch_chat.account_timezone_v1
  FROM PUBLIC,brains_app,lifeswitch_chat_reader_v1,lifeswitch_chat_binding_writer_v1;

CREATE FUNCTION lifeswitch_chat.read_owner_timezone_v1(p_context_id uuid)
RETURNS TABLE(timezone_name text,timezone_source text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
  WITH owner_scope AS (
    SELECT lifeswitch_chat.resolve_owner_read_context_v1(p_context_id) AS owner_user_id
  ), candidates AS (
    SELECT timezone.timezone_name,timezone.source AS timezone_source,1 AS precedence
    FROM lifeswitch_chat.account_timezone_v1 timezone,owner_scope
    WHERE timezone.owner_user_id=owner_scope.owner_user_id
    UNION ALL
    SELECT version.owner_timezone,'active_plan'::text,2
    FROM owner_scope
    JOIN lifeswitch_agentic.plan_owner_state state
      ON state.owner_user_id=owner_scope.owner_user_id
    JOIN lifeswitch_agentic.plan_versions version
      ON version.owner_user_id=state.owner_user_id
     AND version.id=state.active_plan_version_id
    JOIN pg_catalog.pg_timezone_names timezone_catalog
      ON timezone_catalog.name=version.owner_timezone
  )
  SELECT candidates.timezone_name,candidates.timezone_source
  FROM candidates
  ORDER BY precedence
  LIMIT 1
$$;

CREATE FUNCTION lifeswitch_chat.read_plan_v1(p_context_id uuid)
RETURNS TABLE(plan_source text,document jsonb)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
DECLARE
  v_owner_user_id uuid;
  v_document jsonb;
BEGIN
  v_owner_user_id := lifeswitch_chat.resolve_owner_read_context_v1(p_context_id);
  SELECT version.document INTO v_document
  FROM lifeswitch_agentic.plan_owner_state state
  JOIN lifeswitch_agentic.plan_versions version
    ON version.owner_user_id=state.owner_user_id
   AND version.id=state.active_plan_version_id
  WHERE state.owner_user_id=v_owner_user_id;
  IF FOUND THEN
    RETURN QUERY SELECT 'agentic_active'::text,
      lifeswitch_chat.whitelist_plan_document_v1(v_document);
    RETURN;
  END IF;
  SELECT pg_catalog.jsonb_build_object(
    'phase',profile.phase,
    'phase_label',profile.phase_label,
    'primary_goal',profile.primary_goal,
    'start_date',profile.start_date,
    'review_date',profile.review_date,
    'review_cadence',profile.review_cadence,
    'nutrition_targets',profile.nutrition_targets,
    'training_targets',profile.training_targets,
    'conditioning_targets',profile.conditioning_targets,
    'activity_targets',profile.activity_targets,
    'recovery_targets',profile.recovery_targets
  ) INTO v_document
  FROM lifeswitch_plan.plan_profile profile
  WHERE profile.owner_user_id=v_owner_user_id AND profile.is_active=true
  ORDER BY profile.updated_at DESC NULLS LAST
  LIMIT 1;
  IF FOUND THEN
    RETURN QUERY SELECT 'legacy_fallback'::text,
      lifeswitch_chat.whitelist_plan_document_v1(v_document);
  END IF;
END
$$;

CREATE FUNCTION lifeswitch_chat.read_nutrition_daily_v1(
  p_context_id uuid,p_start_date date,p_end_date date
)
RETURNS TABLE(
  day date,entry_count integer,kcal double precision,protein_g double precision,
  carbs_g double precision,fat_g double precision
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
  WITH owner_scope AS (
    SELECT lifeswitch_chat.resolve_owner_read_context_v1(p_context_id) AS owner_user_id
  )
  SELECT
    nutrition_day.day,
    pg_catalog.count(entry.nutrition_entry_id)::integer,
    COALESCE(pg_catalog.sum(CASE WHEN entry.my_food_id IS NOT NULL
      THEN food.kcal*COALESCE(entry.qty_g,serving.grams*entry.qty_servings)/100.0
      ELSE meal_total.kcal END),0)::double precision,
    COALESCE(pg_catalog.sum(CASE WHEN entry.my_food_id IS NOT NULL
      THEN food.protein_g*COALESCE(entry.qty_g,serving.grams*entry.qty_servings)/100.0
      ELSE meal_total.protein_g END),0)::double precision,
    COALESCE(pg_catalog.sum(CASE WHEN entry.my_food_id IS NOT NULL
      THEN food.carbs_g*COALESCE(entry.qty_g,serving.grams*entry.qty_servings)/100.0
      ELSE meal_total.carbs_g END),0)::double precision,
    COALESCE(pg_catalog.sum(CASE WHEN entry.my_food_id IS NOT NULL
      THEN food.fat_g*COALESCE(entry.qty_g,serving.grams*entry.qty_servings)/100.0
      ELSE meal_total.fat_g END),0)::double precision
  FROM owner_scope
  JOIN lifeswitch_nutrition.nutrition_day nutrition_day
    ON nutrition_day.owner_user_id=owner_scope.owner_user_id
  LEFT JOIN lifeswitch_nutrition.nutrition_entry entry
    ON entry.nutrition_day_id=nutrition_day.nutrition_day_id
  LEFT JOIN lifeswitch_nutrition.my_food food ON food.my_food_id=entry.my_food_id
  LEFT JOIN lifeswitch_nutrition.my_food_serving serving
    ON serving.my_food_serving_id=entry.my_food_serving_id
   AND serving.my_food_id=entry.my_food_id
  LEFT JOIN LATERAL (
    SELECT
      pg_catalog.sum(item_food.kcal*COALESCE(item.qty_g,item_serving.grams*item.qty_servings)/100.0) AS kcal,
      pg_catalog.sum(item_food.protein_g*COALESCE(item.qty_g,item_serving.grams*item.qty_servings)/100.0) AS protein_g,
      pg_catalog.sum(item_food.carbs_g*COALESCE(item.qty_g,item_serving.grams*item.qty_servings)/100.0) AS carbs_g,
      pg_catalog.sum(item_food.fat_g*COALESCE(item.qty_g,item_serving.grams*item.qty_servings)/100.0) AS fat_g
    FROM lifeswitch_nutrition.meal_item item
    JOIN lifeswitch_nutrition.my_food item_food ON item_food.my_food_id=item.my_food_id
    LEFT JOIN lifeswitch_nutrition.my_food_serving item_serving
      ON item_serving.my_food_serving_id=item.my_food_serving_id
     AND item_serving.my_food_id=item.my_food_id
    WHERE item.meal_id=entry.meal_id
  ) meal_total ON true
  WHERE nutrition_day.day BETWEEN p_start_date AND p_end_date
  GROUP BY nutrition_day.day
  ORDER BY nutrition_day.day
$$;

CREATE FUNCTION lifeswitch_chat.read_resistance_sessions_v1(
  p_context_id uuid,p_start_date date,p_end_date date
)
RETURNS TABLE(
  day date,active_set_count integer,exercise_count integer,
  strength_set_count integer,strength_exercise_count integer,
  rehab_set_count integer,rehab_exercise_count integer,
  unknown_role_set_count integer,unknown_role_exercise_count integer
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
  WITH owner_scope AS (
    SELECT lifeswitch_chat.resolve_owner_read_context_v1(p_context_id) AS owner_user_id
  )
  SELECT session.day,
    pg_catalog.count(log.training_set_log_id)::integer,
    pg_catalog.count(DISTINCT log.exercise_id)::integer,
    pg_catalog.count(log.training_set_log_id) FILTER (WHERE log.capture_role='strength')::integer,
    pg_catalog.count(DISTINCT log.exercise_id) FILTER (WHERE log.capture_role='strength')::integer,
    pg_catalog.count(log.training_set_log_id) FILTER (WHERE log.capture_role='rehab')::integer,
    pg_catalog.count(DISTINCT log.exercise_id) FILTER (WHERE log.capture_role='rehab')::integer,
    pg_catalog.count(log.training_set_log_id) FILTER (WHERE log.capture_role='unknown')::integer,
    pg_catalog.count(DISTINCT log.exercise_id) FILTER (WHERE log.capture_role='unknown')::integer
  FROM owner_scope
  JOIN lifeswitch_training.training_session_current_v session
    ON session.owner_user_id=owner_scope.owner_user_id
  JOIN lifeswitch_training.training_set_log log
    ON log.training_session_id=session.training_session_id AND log.is_active=true
  WHERE session.finished_at IS NOT NULL
    AND session.day BETWEEN p_start_date AND p_end_date
  GROUP BY session.training_session_id,session.day
  ORDER BY session.day
$$;

CREATE FUNCTION lifeswitch_chat.read_conditioning_sessions_v1(
  p_context_id uuid,p_start_date date,p_end_date date
)
RETURNS TABLE(
  day date,name text,category text,modality text,duration_min numeric,
  intensity text,distance_value numeric,distance_unit text,
  heart_rate_avg numeric,recovery_impact text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
  WITH owner_scope AS (
    SELECT lifeswitch_chat.resolve_owner_read_context_v1(p_context_id) AS owner_user_id
  )
  SELECT session.day,session.name,session.category,session.modality,
    session.duration_min,session.intensity,session.distance_value,
    session.distance_unit,session.heart_rate_avg,session.recovery_impact
  FROM owner_scope
  JOIN lifeswitch_training.conditioning_session_current_v session
    ON session.owner_user_id=owner_scope.owner_user_id
  WHERE session.is_active=true
    AND session.day BETWEEN p_start_date AND p_end_date
  ORDER BY session.day,session.created_at,session.conditioning_session_log_id
  LIMIT 500
$$;

CREATE FUNCTION lifeswitch_chat.read_measurement_observations_v1(
  p_context_id uuid,p_start_date date,p_end_date date
)
RETURNS TABLE(
  local_date date,weight_value numeric,weight_unit text,waist_value numeric,
  body_fat_percent numeric,measurement_unit text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
  WITH owner_scope AS (
    SELECT lifeswitch_chat.resolve_owner_read_context_v1(p_context_id) AS owner_user_id
  )
  SELECT measurement.local_date,measurement.weight_value,measurement.weight_unit,
    measurement.waist_value,measurement.body_fat_percent,measurement.measurement_unit
  FROM owner_scope
  JOIN public.lifeswitch_measurement_entries measurement
    ON measurement.owner_user_id::text=owner_scope.owner_user_id::text
  WHERE measurement.is_active=true
    AND measurement.local_date BETWEEN p_start_date AND p_end_date
  ORDER BY measurement.local_date
  LIMIT 500
$$;

CREATE FUNCTION lifeswitch_chat.read_training_day_v1(
  p_context_id uuid,p_day date
)
RETURNS TABLE(
  day date,name text,set_count integer,exercise_count integer,
  total_volume double precision
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
  WITH owner_scope AS (
    SELECT lifeswitch_chat.resolve_owner_read_context_v1(p_context_id) AS owner_user_id
  )
  SELECT session.day,session.name,
    pg_catalog.count(log.training_set_log_id)::integer,
    pg_catalog.count(DISTINCT log.exercise_id)::integer,
    COALESCE(pg_catalog.sum(log.volume),0)::double precision
  FROM owner_scope
  JOIN lifeswitch_training.training_session_current_v session
    ON session.owner_user_id=owner_scope.owner_user_id
  JOIN lifeswitch_training.training_set_log log
    ON log.training_session_id=session.training_session_id AND log.is_active=true
  WHERE session.finished_at IS NOT NULL AND session.day=p_day
  GROUP BY session.training_session_id,session.day,session.name
  ORDER BY session.name
  LIMIT 50
$$;

CREATE FUNCTION lifeswitch_chat.read_exercise_progression_v1(
  p_context_id uuid,p_start_date date,p_end_date date,p_subject text
)
RETURNS TABLE(
  day date,exercise_name text,set_count integer,total_reps integer,
  max_load double precision,total_volume double precision,load_unit text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
DECLARE
  v_owner_user_id uuid;
BEGIN
  IF pg_catalog.char_length(pg_catalog.btrim(p_subject)) NOT BETWEEN 1 AND 80 THEN
    RAISE EXCEPTION 'invalid exercise subject' USING ERRCODE='22023';
  END IF;
  v_owner_user_id := lifeswitch_chat.resolve_owner_read_context_v1(p_context_id);
  RETURN QUERY
  SELECT session.day,pg_catalog.max(log.exercise_name),
    pg_catalog.count(log.training_set_log_id)::integer,
    COALESCE(pg_catalog.sum(log.reps),0)::integer,
    COALESCE(pg_catalog.max(log.weight),0)::double precision,
    COALESCE(pg_catalog.sum(log.volume),0)::double precision,
    CASE
      WHEN pg_catalog.count(DISTINCT NULLIF(pg_catalog.btrim(log.load_unit),''))=1
        THEN pg_catalog.max(NULLIF(pg_catalog.btrim(log.load_unit),''))
      WHEN pg_catalog.count(DISTINCT NULLIF(pg_catalog.btrim(log.load_unit),''))>1
        THEN 'mixed'::text
      ELSE NULL::text
    END
  FROM lifeswitch_training.training_session_current_v session
  JOIN lifeswitch_training.training_set_log log
    ON log.training_session_id=session.training_session_id AND log.is_active=true
  WHERE session.owner_user_id=v_owner_user_id
    AND session.finished_at IS NOT NULL
    AND session.day BETWEEN p_start_date AND p_end_date
    AND log.capture_role='strength'
    AND pg_catalog.lower(log.exercise_name) LIKE
      ('%'||pg_catalog.lower(pg_catalog.btrim(p_subject))||'%')
  GROUP BY session.training_session_id,session.day,log.exercise_id
  ORDER BY session.day
  LIMIT 200;
END
$$;

DO $$
DECLARE
  function_signature text;
BEGIN
  FOREACH function_signature IN ARRAY ARRAY[
    'lifeswitch_chat.read_owner_timezone_v1(uuid)',
    'lifeswitch_chat.read_plan_v1(uuid)',
    'lifeswitch_chat.read_nutrition_daily_v1(uuid,date,date)',
    'lifeswitch_chat.read_resistance_sessions_v1(uuid,date,date)',
    'lifeswitch_chat.read_conditioning_sessions_v1(uuid,date,date)',
    'lifeswitch_chat.read_measurement_observations_v1(uuid,date,date)',
    'lifeswitch_chat.read_training_day_v1(uuid,date)',
    'lifeswitch_chat.read_exercise_progression_v1(uuid,date,date,text)'
  ] LOOP
    EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO sage',function_signature);
    EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,brains_app',function_signature);
    EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION %s TO lifeswitch_chat_reader_v1',function_signature);
  END LOOP;
END
$$;

/* Explicitly deny all underlying source access to the reader. */
REVOKE ALL ON ALL TABLES IN SCHEMA lifeswitch_agentic,lifeswitch_plan,
  lifeswitch_nutrition,lifeswitch_training,public FROM lifeswitch_chat_reader_v1;
REVOKE ALL ON SCHEMA lifeswitch_agentic,lifeswitch_plan,lifeswitch_nutrition,
  lifeswitch_training,public FROM lifeswitch_chat_reader_v1;

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
  record_refs jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(record_refs)='array'),
  created_at timestamptz NOT NULL,
  persisted_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
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
    owner_user_id=NULLIF(pg_catalog.current_setting('app.lifeswitch_owner_id',true),'')::uuid
  )
  WITH CHECK (
    owner_user_id=NULLIF(pg_catalog.current_setting('app.lifeswitch_owner_id',true),'')::uuid
  );
REVOKE ALL ON lifeswitch_chat.final_answer_lifeswitch_binding_v1
  FROM PUBLIC,brains_app;
GRANT SELECT,INSERT ON lifeswitch_chat.final_answer_lifeswitch_binding_v1
  TO lifeswitch_chat_binding_writer_v1;

CREATE FUNCTION lifeswitch_chat.reject_binding_mutation_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path=''
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
  'Private owner-scoped LifeSwitch chat gateway and answer-audit boundary.';
COMMENT ON TABLE lifeswitch_chat.owner_read_context_v1 IS
  'Short-lived backend-PID-bound owner authority for restricted read gateways.';
COMMENT ON TABLE lifeswitch_chat.account_timezone_v1 IS
  'Backend-authoritative account timezone; active-plan timezone is a bounded fallback.';
COMMENT ON TABLE lifeswitch_chat.final_answer_lifeswitch_binding_v1 IS
  'Append-only answer binding for structured LifeSwitch context; never Memory evidence.';

COMMIT;
