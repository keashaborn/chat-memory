BEGIN;

CREATE FUNCTION lifeswitch_chat.read_lifting_progression_summary_v3(
  p_context_id uuid,p_start_date date,p_end_date date
)
RETURNS TABLE(
  exercise_id text,exercise_name text,exposure_count integer,set_count integer,
  first_day date,last_day date,early_exposure_count integer,recent_exposure_count integer,
  early_max_load double precision,recent_max_load double precision,
  early_reps_per_set double precision,recent_reps_per_set double precision,
  early_volume_per_set double precision,recent_volume_per_set double precision,
  early_sets_per_exposure double precision,recent_sets_per_exposure double precision,
  load_unit text,load_comparable boolean,resolution_sources text[]
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
      COALESCE(pg_catalog.max(base.weight),0)::double precision AS max_load,
      CASE WHEN pg_catalog.count(base.training_set_log_id)>0
        THEN COALESCE(pg_catalog.sum(base.reps),0)::double precision
          / pg_catalog.count(base.training_set_log_id)::double precision
        ELSE NULL::double precision END AS reps_per_set,
      CASE WHEN pg_catalog.count(base.training_set_log_id)>0
        THEN COALESCE(pg_catalog.sum(base.volume),0)::double precision
          / pg_catalog.count(base.training_set_log_id)::double precision
        ELSE NULL::double precision END AS volume_per_set,
      CASE
        WHEN pg_catalog.count(base.load_unit)=pg_catalog.count(*)
          AND pg_catalog.count(DISTINCT base.load_unit)=1
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
      ) AS chronological_rank,
      pg_catalog.count(*) OVER (
        PARTITION BY per_exposure.exercise_id
      ) AS exposure_count
    FROM per_exposure
  ),
  windowed AS (
    SELECT ranked.*,
      CASE
        WHEN ranked.chronological_rank <= pg_catalog.floor(ranked.exposure_count/2.0)
          THEN 'early'::text
        WHEN ranked.chronological_rank > pg_catalog.ceil(ranked.exposure_count/2.0)
          THEN 'recent'::text
        ELSE 'middle'::text
      END AS comparison_window
    FROM ranked
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
    SELECT windowed.exercise_id,
      pg_catalog.max(windowed.exercise_name)::text AS exercise_name,
      pg_catalog.max(windowed.exposure_count)::integer AS exposure_count,
      COALESCE(pg_catalog.sum(windowed.set_count),0)::integer AS set_count,
      pg_catalog.min(windowed.day) AS first_day,
      pg_catalog.max(windowed.day) AS last_day,
      pg_catalog.count(*) FILTER (WHERE windowed.comparison_window='early')::integer
        AS early_exposure_count,
      pg_catalog.count(*) FILTER (WHERE windowed.comparison_window='recent')::integer
        AS recent_exposure_count,
      pg_catalog.avg(windowed.max_load) FILTER (WHERE windowed.comparison_window='early')
        AS early_max_load,
      pg_catalog.avg(windowed.max_load) FILTER (WHERE windowed.comparison_window='recent')
        AS recent_max_load,
      pg_catalog.avg(windowed.reps_per_set) FILTER (WHERE windowed.comparison_window='early')
        AS early_reps_per_set,
      pg_catalog.avg(windowed.reps_per_set) FILTER (WHERE windowed.comparison_window='recent')
        AS recent_reps_per_set,
      pg_catalog.avg(windowed.volume_per_set) FILTER (WHERE windowed.comparison_window='early')
        AS early_volume_per_set,
      pg_catalog.avg(windowed.volume_per_set) FILTER (WHERE windowed.comparison_window='recent')
        AS recent_volume_per_set,
      pg_catalog.avg(windowed.set_count::double precision)
        FILTER (WHERE windowed.comparison_window='early') AS early_sets_per_exposure,
      pg_catalog.avg(windowed.set_count::double precision)
        FILTER (WHERE windowed.comparison_window='recent') AS recent_sets_per_exposure,
      CASE
        WHEN pg_catalog.count(windowed.load_unit)=pg_catalog.count(*)
          AND pg_catalog.count(DISTINCT windowed.load_unit)=1
          AND pg_catalog.max(windowed.load_unit)<>'mixed'
          THEN pg_catalog.max(windowed.load_unit)
        WHEN pg_catalog.count(DISTINCT windowed.load_unit)>0 THEN 'mixed'::text
        ELSE NULL::text
      END AS load_unit,
      (
        pg_catalog.count(windowed.load_unit)=pg_catalog.count(*)
        AND pg_catalog.count(DISTINCT windowed.load_unit)=1
        AND pg_catalog.max(windowed.load_unit)<>'mixed'
      )::boolean AS load_comparable
    FROM windowed
    GROUP BY windowed.exercise_id
  )
  SELECT aggregate_summary.exercise_id,aggregate_summary.exercise_name,
    aggregate_summary.exposure_count,aggregate_summary.set_count,
    aggregate_summary.first_day,aggregate_summary.last_day,
    aggregate_summary.early_exposure_count,aggregate_summary.recent_exposure_count,
    aggregate_summary.early_max_load,aggregate_summary.recent_max_load,
    aggregate_summary.early_reps_per_set,aggregate_summary.recent_reps_per_set,
    aggregate_summary.early_volume_per_set,aggregate_summary.recent_volume_per_set,
    aggregate_summary.early_sets_per_exposure,aggregate_summary.recent_sets_per_exposure,
    aggregate_summary.load_unit,aggregate_summary.load_comparable,
    source_summary.resolution_sources
  FROM aggregate_summary
  JOIN source_summary USING (exercise_id)
  ORDER BY aggregate_summary.exposure_count DESC,
    aggregate_summary.set_count DESC,aggregate_summary.exercise_name,
    aggregate_summary.exercise_id
  LIMIT 12;
END
$$;

ALTER FUNCTION lifeswitch_chat.read_lifting_progression_summary_v3(uuid,date,date)
  OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.read_lifting_progression_summary_v3(uuid,date,date)
  FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION lifeswitch_chat.read_lifting_progression_summary_v3(uuid,date,date)
  TO lifeswitch_chat_reader_v1;

COMMENT ON FUNCTION lifeswitch_chat.read_lifting_progression_summary_v3(uuid,date,date) IS
  'Owner-bound strength micro-analysis comparing early and recent exposure windows.';

COMMIT;
