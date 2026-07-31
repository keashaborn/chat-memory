BEGIN;

CREATE FUNCTION lifeswitch_chat.read_exercise_frequency_v1(
  p_context_id uuid,p_start_date date,p_end_date date
)
RETURNS TABLE(
  exercise_id text,exercise_name text,effective_role text,
  set_count integer,session_count integer,first_day date,last_day date,
  resolution_sources text[],role_conflict boolean
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
DECLARE
  v_owner_user_id uuid;
BEGIN
  IF p_start_date IS NULL OR p_end_date IS NULL
     OR p_end_date < p_start_date OR (p_end_date-p_start_date)>366 THEN
    RAISE EXCEPTION 'invalid exercise frequency window' USING ERRCODE='22023';
  END IF;
  v_owner_user_id := lifeswitch_chat.resolve_owner_read_context_v1(p_context_id);
  RETURN QUERY
  SELECT log.exercise_id::text,
    pg_catalog.left(pg_catalog.max(log.exercise_name),80)::text,
    role_resolution.effective_role,
    pg_catalog.count(log.training_set_log_id)::integer,
    pg_catalog.count(DISTINCT session.training_session_id)::integer,
    pg_catalog.min(session.day),
    pg_catalog.max(session.day),
    pg_catalog.array_agg(
      DISTINCT role_resolution.resolution_source
      ORDER BY role_resolution.resolution_source
    )::text[],
    pg_catalog.bool_or(role_resolution.role_conflict)
  FROM lifeswitch_training.training_session_current_v session
  JOIN lifeswitch_training.training_set_log log
    ON log.training_session_id=session.training_session_id
   AND log.owner_user_id=session.owner_user_id
   AND log.is_active=true
  JOIN lifeswitch_training.training_set_effective_role_v1 role_resolution
    ON role_resolution.training_set_log_id=log.training_set_log_id
   AND role_resolution.training_session_id=log.training_session_id
   AND role_resolution.owner_user_id=log.owner_user_id
  WHERE session.owner_user_id=v_owner_user_id
    AND session.finished_at IS NOT NULL
    AND session.day BETWEEN p_start_date AND p_end_date
  GROUP BY log.exercise_id,role_resolution.effective_role
  ORDER BY pg_catalog.count(log.training_set_log_id) DESC,
    pg_catalog.count(DISTINCT session.training_session_id) DESC,
    pg_catalog.max(log.exercise_name),log.exercise_id
  LIMIT 12;
END
$$;

CREATE FUNCTION lifeswitch_chat.read_lifting_progression_summary_v1(
  p_context_id uuid,p_start_date date,p_end_date date
)
RETURNS TABLE(
  exercise_id text,exercise_name text,exposure_count integer,set_count integer,
  first_day date,last_day date,first_max_load double precision,
  latest_max_load double precision,first_total_reps integer,
  latest_total_reps integer,first_total_volume double precision,
  latest_total_volume double precision,first_load_unit text,
  latest_load_unit text,resolution_sources text[]
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
DECLARE
  v_owner_user_id uuid;
BEGIN
  IF p_start_date IS NULL OR p_end_date IS NULL
     OR p_end_date < p_start_date OR (p_end_date-p_start_date)>366 THEN
    RAISE EXCEPTION 'invalid lifting progression window' USING ERRCODE='22023';
  END IF;
  v_owner_user_id := lifeswitch_chat.resolve_owner_read_context_v1(p_context_id);
  RETURN QUERY
  WITH base AS (
    SELECT session.training_session_id,session.day,log.exercise_id::text,
      log.exercise_name,log.training_set_log_id,log.reps,log.weight,log.volume,
      NULLIF(pg_catalog.btrim(log.load_unit),'') AS load_unit,
      role_resolution.resolution_source
    FROM lifeswitch_training.training_session_current_v session
    JOIN lifeswitch_training.training_set_log log
      ON log.training_session_id=session.training_session_id
     AND log.owner_user_id=session.owner_user_id
     AND log.is_active=true
    JOIN lifeswitch_training.training_set_effective_role_v1 role_resolution
      ON role_resolution.training_set_log_id=log.training_set_log_id
     AND role_resolution.training_session_id=log.training_session_id
     AND role_resolution.owner_user_id=log.owner_user_id
    WHERE session.owner_user_id=v_owner_user_id
      AND session.finished_at IS NOT NULL
      AND session.day BETWEEN p_start_date AND p_end_date
      AND role_resolution.effective_role='strength'
  ),
  per_exposure AS (
    SELECT base.exercise_id,
      pg_catalog.left(pg_catalog.max(base.exercise_name),80)::text AS exercise_name,
      base.training_session_id,base.day,
      pg_catalog.count(base.training_set_log_id)::integer AS set_count,
      COALESCE(pg_catalog.sum(base.reps),0)::integer AS total_reps,
      COALESCE(pg_catalog.max(base.weight),0)::double precision AS max_load,
      COALESCE(pg_catalog.sum(base.volume),0)::double precision AS total_volume,
      CASE
        WHEN pg_catalog.count(DISTINCT base.load_unit)=1
          THEN pg_catalog.max(base.load_unit)
        WHEN pg_catalog.count(DISTINCT base.load_unit)>1 THEN 'mixed'::text
        ELSE NULL::text
      END AS load_unit
    FROM base
    GROUP BY base.exercise_id,base.training_session_id,base.day
  ),
  ranked AS (
    SELECT per_exposure.*,
      pg_catalog.row_number() OVER (
        PARTITION BY per_exposure.exercise_id
        ORDER BY per_exposure.day,per_exposure.training_session_id
      ) AS first_rank,
      pg_catalog.row_number() OVER (
        PARTITION BY per_exposure.exercise_id
        ORDER BY per_exposure.day DESC,per_exposure.training_session_id DESC
      ) AS latest_rank
    FROM per_exposure
  ),
  source_summary AS (
    SELECT base.exercise_id,
      pg_catalog.array_agg(
        DISTINCT base.resolution_source ORDER BY base.resolution_source
      )::text[] AS resolution_sources
    FROM base
    GROUP BY base.exercise_id
  ),
  aggregate_summary AS (
    SELECT ranked.exercise_id,pg_catalog.max(ranked.exercise_name)::text AS exercise_name,
      pg_catalog.count(*)::integer AS exposure_count,
      COALESCE(pg_catalog.sum(ranked.set_count),0)::integer AS set_count,
      pg_catalog.max(ranked.day) FILTER (WHERE ranked.first_rank=1) AS first_day,
      pg_catalog.max(ranked.day) FILTER (WHERE ranked.latest_rank=1) AS last_day,
      pg_catalog.max(ranked.max_load) FILTER (WHERE ranked.first_rank=1) AS first_max_load,
      pg_catalog.max(ranked.max_load) FILTER (WHERE ranked.latest_rank=1) AS latest_max_load,
      pg_catalog.max(ranked.total_reps) FILTER (WHERE ranked.first_rank=1)::integer AS first_total_reps,
      pg_catalog.max(ranked.total_reps) FILTER (WHERE ranked.latest_rank=1)::integer AS latest_total_reps,
      pg_catalog.max(ranked.total_volume) FILTER (WHERE ranked.first_rank=1) AS first_total_volume,
      pg_catalog.max(ranked.total_volume) FILTER (WHERE ranked.latest_rank=1) AS latest_total_volume,
      pg_catalog.max(ranked.load_unit) FILTER (WHERE ranked.first_rank=1) AS first_load_unit,
      pg_catalog.max(ranked.load_unit) FILTER (WHERE ranked.latest_rank=1) AS latest_load_unit
    FROM ranked
    GROUP BY ranked.exercise_id
  )
  SELECT aggregate_summary.exercise_id,aggregate_summary.exercise_name,
    aggregate_summary.exposure_count,aggregate_summary.set_count,
    aggregate_summary.first_day,aggregate_summary.last_day,
    aggregate_summary.first_max_load,aggregate_summary.latest_max_load,
    aggregate_summary.first_total_reps,aggregate_summary.latest_total_reps,
    aggregate_summary.first_total_volume,aggregate_summary.latest_total_volume,
    aggregate_summary.first_load_unit,aggregate_summary.latest_load_unit,
    source_summary.resolution_sources
  FROM aggregate_summary
  JOIN source_summary USING (exercise_id)
  ORDER BY aggregate_summary.exposure_count DESC,
    aggregate_summary.set_count DESC,aggregate_summary.exercise_name,
    aggregate_summary.exercise_id
  LIMIT 12;
END
$$;

ALTER FUNCTION lifeswitch_chat.read_exercise_frequency_v1(uuid,date,date)
  OWNER TO sage;
ALTER FUNCTION lifeswitch_chat.read_lifting_progression_summary_v1(uuid,date,date)
  OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.read_exercise_frequency_v1(uuid,date,date)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION lifeswitch_chat.read_lifting_progression_summary_v1(uuid,date,date)
  FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION lifeswitch_chat.read_exercise_frequency_v1(uuid,date,date)
  TO lifeswitch_chat_reader_v1;
GRANT EXECUTE ON FUNCTION lifeswitch_chat.read_lifting_progression_summary_v1(uuid,date,date)
  TO lifeswitch_chat_reader_v1;

COMMENT ON FUNCTION lifeswitch_chat.read_exercise_frequency_v1(uuid,date,date) IS
  'Owner-bound bounded exercise-frequency projection for LifeSwitch chat.';
COMMENT ON FUNCTION lifeswitch_chat.read_lifting_progression_summary_v1(uuid,date,date) IS
  'Owner-bound bounded strength-progression summary for LifeSwitch chat.';

COMMIT;
