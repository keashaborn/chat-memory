BEGIN;

DO $role_check$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'lifeswitch_training_observation_owner'
      AND rolcanlogin = false
  ) THEN
    RAISE EXCEPTION
      'lifeswitch_training_observation_owner must exist as a NOLOGIN role';
  END IF;
END;
$role_check$;

-- SECURITY DEFINER writers execute as this NOLOGIN owner.  Keep raw write
-- privileges away from application roles while granting the owner only the
-- operations required by the governed create/correct/void functions.
GRANT SELECT, INSERT, UPDATE
  ON TABLE lifeswitch_training.training_session
  TO lifeswitch_training_observation_owner;

GRANT SELECT, INSERT
  ON TABLE
    lifeswitch_training.training_set_log,
    lifeswitch_training.training_set_log_segment,
    lifeswitch_training.training_observation_event
  TO lifeswitch_training_observation_owner;

GRANT SELECT, INSERT, UPDATE
  ON TABLE lifeswitch_training.conditioning_session_log
  TO lifeswitch_training_observation_owner;

DO $postflight$
DECLARE
  v_owner constant text := 'lifeswitch_training_observation_owner';
BEGIN
  IF NOT has_table_privilege(v_owner, 'lifeswitch_training.training_session', 'SELECT')
     OR NOT has_table_privilege(v_owner, 'lifeswitch_training.training_session', 'INSERT')
     OR NOT has_table_privilege(v_owner, 'lifeswitch_training.training_session', 'UPDATE')
     OR NOT has_table_privilege(v_owner, 'lifeswitch_training.training_set_log', 'SELECT')
     OR NOT has_table_privilege(v_owner, 'lifeswitch_training.training_set_log', 'INSERT')
     OR NOT has_table_privilege(v_owner, 'lifeswitch_training.training_set_log_segment', 'SELECT')
     OR NOT has_table_privilege(v_owner, 'lifeswitch_training.training_set_log_segment', 'INSERT')
     OR NOT has_table_privilege(v_owner, 'lifeswitch_training.conditioning_session_log', 'SELECT')
     OR NOT has_table_privilege(v_owner, 'lifeswitch_training.conditioning_session_log', 'INSERT')
     OR NOT has_table_privilege(v_owner, 'lifeswitch_training.conditioning_session_log', 'UPDATE')
     OR NOT has_table_privilege(v_owner, 'lifeswitch_training.training_observation_event', 'SELECT')
     OR NOT has_table_privilege(v_owner, 'lifeswitch_training.training_observation_event', 'INSERT') THEN
    RAISE EXCEPTION 'Training writer owner is missing a required table privilege';
  END IF;

  IF has_table_privilege(v_owner, 'lifeswitch_training.training_session', 'DELETE')
     OR has_table_privilege(v_owner, 'lifeswitch_training.training_session', 'TRUNCATE')
     OR has_table_privilege(v_owner, 'lifeswitch_training.training_set_log', 'UPDATE')
     OR has_table_privilege(v_owner, 'lifeswitch_training.training_set_log', 'DELETE')
     OR has_table_privilege(v_owner, 'lifeswitch_training.training_set_log', 'TRUNCATE')
     OR has_table_privilege(v_owner, 'lifeswitch_training.training_set_log_segment', 'UPDATE')
     OR has_table_privilege(v_owner, 'lifeswitch_training.training_set_log_segment', 'DELETE')
     OR has_table_privilege(v_owner, 'lifeswitch_training.training_set_log_segment', 'TRUNCATE')
     OR has_table_privilege(v_owner, 'lifeswitch_training.conditioning_session_log', 'DELETE')
     OR has_table_privilege(v_owner, 'lifeswitch_training.conditioning_session_log', 'TRUNCATE')
     OR has_table_privilege(v_owner, 'lifeswitch_training.training_observation_event', 'UPDATE')
     OR has_table_privilege(v_owner, 'lifeswitch_training.training_observation_event', 'DELETE')
     OR has_table_privilege(v_owner, 'lifeswitch_training.training_observation_event', 'TRUNCATE') THEN
    RAISE EXCEPTION 'Training writer owner has an unnecessary destructive privilege';
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brains_app') THEN
    IF has_table_privilege('brains_app', 'lifeswitch_training.training_session', 'INSERT')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_session', 'UPDATE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_session', 'DELETE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_session', 'TRUNCATE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_set_log', 'INSERT')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_set_log', 'UPDATE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_set_log', 'DELETE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_set_log', 'TRUNCATE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_set_log_segment', 'INSERT')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_set_log_segment', 'UPDATE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_set_log_segment', 'DELETE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_set_log_segment', 'TRUNCATE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.conditioning_session_log', 'INSERT')
       OR has_table_privilege('brains_app', 'lifeswitch_training.conditioning_session_log', 'UPDATE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.conditioning_session_log', 'DELETE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.conditioning_session_log', 'TRUNCATE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_observation_event', 'INSERT')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_observation_event', 'UPDATE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_observation_event', 'DELETE')
       OR has_table_privilege('brains_app', 'lifeswitch_training.training_observation_event', 'TRUNCATE') THEN
      RAISE EXCEPTION 'brains_app retains a raw Training write privilege';
    END IF;
  END IF;
END;
$postflight$;

COMMIT;
