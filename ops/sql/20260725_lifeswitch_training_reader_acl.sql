BEGIN;

DO $role_check$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'brains_app'
      AND rolcanlogin = true
  ) THEN
    RAISE EXCEPTION 'brains_app login role is required';
  END IF;
END;
$role_check$;

-- The application reads completed sessions through the governed current views,
-- then joins immutable set/segment observations for deterministic summaries.
-- Restore read access only; all writes remain behind SECURITY DEFINER writers.
GRANT USAGE ON SCHEMA lifeswitch_training TO brains_app;
GRANT SELECT ON TABLE
  lifeswitch_training.training_session,
  lifeswitch_training.training_set_log,
  lifeswitch_training.training_set_log_segment,
  lifeswitch_training.conditioning_session_log,
  lifeswitch_training.training_observation_event,
  lifeswitch_training.training_session_current_v,
  lifeswitch_training.conditioning_session_current_v
TO brains_app;

DO $postflight$
DECLARE
  v_relation text;
BEGIN
  FOREACH v_relation IN ARRAY ARRAY[
    'lifeswitch_training.training_session',
    'lifeswitch_training.training_set_log',
    'lifeswitch_training.training_set_log_segment',
    'lifeswitch_training.conditioning_session_log',
    'lifeswitch_training.training_observation_event',
    'lifeswitch_training.training_session_current_v',
    'lifeswitch_training.conditioning_session_current_v'
  ]
  LOOP
    IF NOT has_table_privilege('brains_app', v_relation, 'SELECT') THEN
      RAISE EXCEPTION 'brains_app is missing SELECT on %', v_relation;
    END IF;
  END LOOP;

  FOREACH v_relation IN ARRAY ARRAY[
    'lifeswitch_training.training_session',
    'lifeswitch_training.training_set_log',
    'lifeswitch_training.training_set_log_segment',
    'lifeswitch_training.conditioning_session_log',
    'lifeswitch_training.training_observation_event'
  ]
  LOOP
    IF has_table_privilege('brains_app', v_relation, 'INSERT')
       OR has_table_privilege('brains_app', v_relation, 'UPDATE')
       OR has_table_privilege('brains_app', v_relation, 'DELETE')
       OR has_table_privilege('brains_app', v_relation, 'TRUNCATE')
       OR has_any_column_privilege('brains_app', v_relation, 'INSERT')
       OR has_any_column_privilege('brains_app', v_relation, 'UPDATE') THEN
      RAISE EXCEPTION 'brains_app retains a raw write privilege on %', v_relation;
    END IF;
  END LOOP;
END;
$postflight$;

COMMIT;
