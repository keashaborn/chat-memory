\set ON_ERROR_STOP on

BEGIN;

DO $test$
DECLARE
  v_owner_user_id uuid := gen_random_uuid();
  v_other_owner_user_id uuid := gen_random_uuid();
  v_session_id uuid;
  v_replacement_session_id uuid;
  v_set_id uuid;
  v_segment_id uuid;
  v_conditioning_id uuid;
  v_replacement_conditioning_id uuid;
  v_event_id uuid;
  v_rejected boolean;
  v_count integer;
BEGIN
  INSERT INTO lifeswitch_training.training_session (
    owner_user_id,
    day,
    name,
    idempotency_key
  ) VALUES (
    v_owner_user_id,
    DATE '2026-07-21',
    'Observation integrity test',
    'training-integrity-test-1'
  )
  RETURNING training_session_id INTO v_session_id;

  SELECT count(*)
    INTO v_count
  FROM lifeswitch_training.training_session
  WHERE training_session_id = v_session_id
    AND recorded_by_user_id = v_owner_user_id
    AND snapshot_schema_version = 1
    AND snapshot_quality = 'captured'
    AND source_snapshot ->> 'kind' = 'training_session';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'training session provenance was not captured';
  END IF;

  SELECT count(*)
    INTO v_count
  FROM lifeswitch_training.training_observation_event
  WHERE observation_type = 'training_session'
    AND observation_id = v_session_id
    AND owner_user_id = v_owner_user_id
    AND event_type = 'created';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'training session creation was not audited';
  END IF;

  v_rejected := false;
  BEGIN
    INSERT INTO lifeswitch_training.training_session (
      owner_user_id,
      day,
      name,
      idempotency_key
    ) VALUES (
      v_owner_user_id,
      DATE '2026-07-21',
      'Duplicate request',
      'training-integrity-test-1'
    );
  EXCEPTION WHEN unique_violation THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'duplicate training idempotency key was accepted';
  END IF;

  INSERT INTO lifeswitch_training.training_set_log (
    training_session_id,
    owner_user_id,
    exercise_id,
    exercise_name,
    weight,
    reps,
    volume,
    exercise_role_snapshot
  ) VALUES (
    v_session_id,
    v_owner_user_id,
    'integrity-rehab-exercise',
    'Integrity rehab exercise',
    25,
    12,
    300,
    'rehab'
  )
  RETURNING training_set_log_id INTO v_set_id;

  SELECT count(*)
    INTO v_count
  FROM lifeswitch_training.training_set_log
  WHERE training_set_log_id = v_set_id
    AND capture_role = 'rehab'
    AND load_unit = 'lb'
    AND snapshot_schema_version = 1
    AND snapshot_quality = 'captured'
    AND source_snapshot ->> 'kind' = 'training_set';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'training set capture role or provenance is invalid';
  END IF;

  v_rejected := false;
  BEGIN
    INSERT INTO lifeswitch_training.training_set_log (
      training_session_id,
      owner_user_id,
      exercise_id,
      exercise_name
    ) VALUES (
      v_session_id,
      v_other_owner_user_id,
      'cross-owner-exercise',
      'Cross-owner exercise'
    );
  EXCEPTION WHEN others THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'cross-owner training set was accepted';
  END IF;

  INSERT INTO lifeswitch_training.training_set_log_segment (
    training_set_log_id,
    segment_index,
    weight,
    reps,
    volume
  ) VALUES (
    v_set_id,
    1,
    20,
    5,
    100
  )
  RETURNING training_set_log_segment_id INTO v_segment_id;

  SELECT count(*)
    INTO v_count
  FROM lifeswitch_training.training_set_log_segment
  WHERE training_set_log_segment_id = v_segment_id
    AND owner_user_id = v_owner_user_id
    AND load_unit = 'lb'
    AND snapshot_schema_version = 1
    AND snapshot_quality = 'captured'
    AND source_snapshot ->> 'kind' = 'training_set_segment';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'training segment ownership or provenance is invalid';
  END IF;

  INSERT INTO lifeswitch_training.training_session (
    owner_user_id,
    day,
    name,
    idempotency_key,
    supersedes_training_session_id
  ) VALUES (
    v_owner_user_id,
    DATE '2026-07-21',
    'Corrected observation integrity test',
    'training-integrity-test-2',
    v_session_id
  )
  RETURNING training_session_id INTO v_replacement_session_id;

  SELECT count(*)
    INTO v_count
  FROM lifeswitch_training.training_session_current_v
  WHERE training_session_id = v_replacement_session_id;
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'replacement training session is absent from current view';
  END IF;

  SELECT count(*)
    INTO v_count
  FROM lifeswitch_training.training_session_current_v
  WHERE training_session_id = v_session_id;
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'superseded training session remains in current view';
  END IF;

  v_rejected := false;
  BEGIN
    INSERT INTO lifeswitch_training.training_session (
      owner_user_id,
      day,
      name,
      idempotency_key,
      supersedes_training_session_id
    ) VALUES (
      v_other_owner_user_id,
      DATE '2026-07-21',
      'Cross-owner correction',
      'training-integrity-cross-owner',
      v_session_id
    );
  EXCEPTION WHEN others THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'cross-owner training correction was accepted';
  END IF;

  UPDATE lifeswitch_training.training_session
  SET
    voided_at = clock_timestamp(),
    voided_by_user_id = v_owner_user_id,
    void_reason = 'runtime test void'
  WHERE training_session_id = v_replacement_session_id;

  SELECT count(*)
    INTO v_count
  FROM lifeswitch_training.training_observation_event
  WHERE observation_type = 'training_session'
    AND observation_id = v_replacement_session_id
    AND event_type = 'voided'
    AND actor_user_id = v_owner_user_id
    AND reason = 'runtime test void';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'training session void was not audited';
  END IF;

  INSERT INTO lifeswitch_training.conditioning_session_log (
    owner_user_id,
    day,
    name,
    duration_min,
    distance_value,
    distance_unit,
    idempotency_key
  ) VALUES (
    v_owner_user_id,
    DATE '2026-07-21',
    'Conditioning integrity test',
    20,
    1.25,
    'mi',
    'conditioning-integrity-test-1'
  )
  RETURNING conditioning_session_log_id INTO v_conditioning_id;

  SELECT count(*)
    INTO v_count
  FROM lifeswitch_training.conditioning_session_log
  WHERE conditioning_session_log_id = v_conditioning_id
    AND recorded_by_user_id = v_owner_user_id
    AND distance_value = 1.25
    AND distance_unit = 'mi'
    AND snapshot_schema_version = 1
    AND snapshot_quality = 'captured'
    AND source_snapshot ->> 'kind' = 'conditioning_session';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'conditioning provenance or typed distance is invalid';
  END IF;

  INSERT INTO lifeswitch_training.conditioning_session_log (
    owner_user_id,
    day,
    name,
    duration_min,
    distance_value,
    distance_unit,
    idempotency_key,
    supersedes_conditioning_session_id
  ) VALUES (
    v_owner_user_id,
    DATE '2026-07-21',
    'Corrected conditioning integrity test',
    21,
    1.4,
    'mi',
    'conditioning-integrity-test-2',
    v_conditioning_id
  )
  RETURNING conditioning_session_log_id
    INTO v_replacement_conditioning_id;

  SELECT count(*)
    INTO v_count
  FROM lifeswitch_training.conditioning_session_current_v
  WHERE conditioning_session_log_id = v_replacement_conditioning_id;
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'replacement conditioning session is absent from current view';
  END IF;

  SELECT training_observation_event_id
    INTO v_event_id
  FROM lifeswitch_training.training_observation_event
  WHERE observation_type = 'conditioning_session'
    AND observation_id = v_replacement_conditioning_id
    AND event_type = 'corrected';
  IF v_event_id IS NULL THEN
    RAISE EXCEPTION 'conditioning correction was not audited';
  END IF;

  v_rejected := false;
  BEGIN
    UPDATE lifeswitch_training.training_observation_event
    SET metadata = metadata || '{"tampered": true}'::jsonb
    WHERE training_observation_event_id = v_event_id;
  EXCEPTION WHEN others THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'audit event update was accepted';
  END IF;

  v_rejected := false;
  BEGIN
    EXECUTE 'TRUNCATE TABLE lifeswitch_training.training_session';
  EXCEPTION WHEN others THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'training observation truncate was accepted';
  END IF;

  DELETE FROM lifeswitch_training.training_set_log
  WHERE training_set_log_id = v_set_id;

  SELECT count(*)
    INTO v_count
  FROM lifeswitch_training.training_observation_event
  WHERE observation_type = 'training_set'
    AND observation_id = v_set_id
    AND event_type = 'legacy_deleted';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'legacy delete audit did not survive row deletion';
  END IF;
END
$test$;

ROLLBACK;

SELECT 'training observation integrity runtime checks passed' AS result;
