BEGIN;

CREATE OR REPLACE FUNCTION lifeswitch_chat.read_resistance_sessions_v1(
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

CREATE OR REPLACE FUNCTION lifeswitch_chat.read_exercise_progression_v1(
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

ALTER FUNCTION lifeswitch_chat.read_resistance_sessions_v1(uuid,date,date)
  OWNER TO sage;
ALTER FUNCTION lifeswitch_chat.read_exercise_progression_v1(uuid,date,date,text)
  OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.read_resistance_sessions_v1(uuid,date,date)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION lifeswitch_chat.read_exercise_progression_v1(uuid,date,date,text)
  FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION lifeswitch_chat.read_resistance_sessions_v1(uuid,date,date)
  TO lifeswitch_chat_reader_v1;
GRANT EXECUTE ON FUNCTION lifeswitch_chat.read_exercise_progression_v1(uuid,date,date,text)
  TO lifeswitch_chat_reader_v1;

COMMIT;
