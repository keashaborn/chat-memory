\set ON_ERROR_STOP on

BEGIN;

CREATE OR REPLACE FUNCTION lifeswitch_chat.whitelist_target_value_v1(
  p_value jsonb
)
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

CREATE OR REPLACE FUNCTION lifeswitch_chat.whitelist_target_section_v1(
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

CREATE OR REPLACE FUNCTION lifeswitch_chat.whitelist_plan_document_v1(
  p_document jsonb
)
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

ALTER FUNCTION lifeswitch_chat.whitelist_target_value_v1(jsonb)
  OWNER TO lifeswitch_owner;
ALTER FUNCTION lifeswitch_chat.whitelist_target_section_v1(jsonb,text[])
  OWNER TO lifeswitch_owner;
ALTER FUNCTION lifeswitch_chat.whitelist_plan_document_v1(jsonb)
  OWNER TO lifeswitch_owner;
REVOKE ALL ON FUNCTION lifeswitch_chat.whitelist_target_value_v1(jsonb)
  FROM PUBLIC,brains_app,lifeswitch_chat_reader;
REVOKE ALL ON FUNCTION lifeswitch_chat.whitelist_target_section_v1(jsonb,text[])
  FROM PUBLIC,brains_app,lifeswitch_chat_reader;
REVOKE ALL ON FUNCTION lifeswitch_chat.whitelist_plan_document_v1(jsonb)
  FROM PUBLIC,brains_app,lifeswitch_chat_reader;

CREATE OR REPLACE FUNCTION lifeswitch_chat.read_plan_v1(p_context_id uuid)
RETURNS TABLE(plan_source text,document jsonb)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
  WITH owner_scope AS (
    SELECT lifeswitch_chat.resolve_owner_read_context_v1(p_context_id) AS owner_user_id
  )
  SELECT
    'canonical_plan'::text,
    lifeswitch_chat.whitelist_plan_document_v1(
      pg_catalog.jsonb_build_object(
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
      )
    )
  FROM owner_scope
  JOIN lifeswitch_plan.plan_profile profile
    ON profile.owner_user_id=owner_scope.owner_user_id
  WHERE profile.is_active=true
  ORDER BY profile.updated_at DESC NULLS LAST
  LIMIT 1
$$;

ALTER FUNCTION lifeswitch_chat.read_plan_v1(uuid) OWNER TO lifeswitch_owner;
REVOKE ALL ON FUNCTION lifeswitch_chat.read_plan_v1(uuid) FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION lifeswitch_chat.read_plan_v1(uuid)
  TO lifeswitch_chat_reader;

COMMENT ON FUNCTION lifeswitch_chat.read_plan_v1(uuid) IS
  'Owner-bound canonical Plan projection from lifeswitch_plan.plan_profile only.';

COMMIT;
