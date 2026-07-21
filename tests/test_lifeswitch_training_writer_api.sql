\set ON_ERROR_STOP on

BEGIN;

-- Fixtures are configuration/reference rows, not observations. The runtime
-- harness loads them as the migration superuser before assuming brains_app.
INSERT INTO lifeswitch_training.my_exercise (
  my_exercise_id,
  owner_user_id,
  exercise_id,
  display_name,
  kind,
  modality,
  exercise_role
) VALUES
  (
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',
    '11111111-1111-4111-8111-111111111111',
    'writer-rehab-band',
    'Writer Rehab Band',
    'custom',
    'band',
    'rehab'
  ),
  (
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2',
    '22222222-2222-4222-8222-222222222222',
    'other-owner-exercise',
    'Other Owner Exercise',
    'custom',
    'free_weight',
    'strength'
  );

INSERT INTO lifeswitch_training.workout_template (
  workout_template_id,
  owner_user_id,
  name
) VALUES (
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1',
  '11111111-1111-4111-8111-111111111111',
  'Writer Test Template'
);

INSERT INTO lifeswitch_training.my_conditioning_prescription (
  my_conditioning_prescription_id,
  owner_user_id,
  name,
  category,
  modality,
  target_duration_min,
  target_frequency_per_week,
  target_intensity
) VALUES (
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc1',
  '11111111-1111-4111-8111-111111111111',
  'Writer Test Walk',
  'low_intensity_cardio',
  'walking',
  20,
  2,
  'easy'
);

SET LOCAL ROLE brains_app;

DO $test$
DECLARE
  v_owner constant uuid := '11111111-1111-4111-8111-111111111111';
  v_other_owner constant uuid := '22222222-2222-4222-8222-222222222222';
  v_training_intent jsonb;
  v_training_correction jsonb;
  v_training_id uuid;
  v_training_replay_id uuid;
  v_training_replacement_id uuid;
  v_conditioning_intent jsonb;
  v_conditioning_correction jsonb;
  v_conditioning_id uuid;
  v_conditioning_replay_id uuid;
  v_conditioning_replacement_id uuid;
  v_rejected boolean;
  v_count integer;
BEGIN
  PERFORM set_config('app.user_id', '', true);
  v_rejected := false;
  BEGIN
    PERFORM lifeswitch_training.create_training_session(
      jsonb_build_object(
        'day', '2026-07-21',
        'name', 'Missing actor test',
        'load_unit', 'lb',
        'sets', jsonb_build_array(
          jsonb_build_object(
            'exercise_id', 'writer-rehab-band',
            'exercise_sort_order', 1,
            'set_index', 1,
            'weight', 20,
            'reps', 10
          )
        )
      ),
      'writer-missing-actor'
    );
  EXCEPTION WHEN invalid_authorization_specification THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'writer accepted a request without transaction-local app.user_id';
  END IF;

  PERFORM set_config('app.user_id', v_owner::text, true);

  v_rejected := false;
  BEGIN
    INSERT INTO lifeswitch_training.training_session (
      owner_user_id,
      day,
      name
    ) VALUES (
      v_owner,
      DATE '2026-07-21',
      'Forbidden raw insert'
    );
  EXCEPTION WHEN insufficient_privilege THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'brains_app retained direct Training observation inserts';
  END IF;

  v_rejected := false;
  BEGIN
    PERFORM lifeswitch_training.create_training_session(
      jsonb_build_object(
        'day', '2026-07-21',
        'name', 'Missing sets test',
        'load_unit', 'lb'
      ),
      'writer-missing-sets'
    );
  EXCEPTION WHEN invalid_parameter_value THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'writer accepted a Training session without sets';
  END IF;

  v_training_intent := jsonb_build_object(
    'day', '2026-07-21',
    'workout_template_id', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1',
    'name', 'Writer Training Session',
    'notes', 'created through protected writer',
    'load_unit', 'lb',
    'sets', jsonb_build_array(
      jsonb_build_object(
        'exercise_id', 'writer-rehab-band',
        'exercise_sort_order', 1,
        'set_index', 1,
        'set_type', 'drop_set',
        'weight', 25,
        'reps', 8,
        'segments', jsonb_build_array(
          jsonb_build_object(
            'segment_index', 1,
            'label', 'work',
            'weight', 20,
            'reps', 5
          ),
          jsonb_build_object(
            'segment_index', 2,
            'label', 'drop',
            'weight', 10,
            'reps', 3
          )
        )
      )
    )
  );

  v_training_id := lifeswitch_training.create_training_session(
    v_training_intent,
    'writer-training-create-1'
  );
  v_training_replay_id := lifeswitch_training.create_training_session(
    v_training_intent,
    'writer-training-create-1'
  );
  IF v_training_replay_id IS DISTINCT FROM v_training_id THEN
    RAISE EXCEPTION 'exact Training idempotency replay returned a different observation';
  END IF;

  SELECT count(*) INTO v_count
  FROM lifeswitch_training.training_session AS s
  JOIN lifeswitch_training.training_set_log AS l
    ON l.training_session_id = s.training_session_id
  WHERE s.training_session_id = v_training_id
    AND s.owner_user_id = v_owner
    AND s.recorded_by_user_id = v_owner
    AND s.snapshot_schema_version = 2
    AND s.snapshot_quality = 'captured'
    AND l.exercise_name = 'Writer Rehab Band'
    AND l.capture_role = 'rehab'
    AND l.exercise_role_snapshot = 'rehab'
    AND l.load_unit = 'lb'
    AND l.volume = 130
    AND l.snapshot_schema_version = 2;
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'Training writer failed provenance, role, unit, or derived-volume capture';
  END IF;

  SELECT count(*) INTO v_count
  FROM lifeswitch_training.training_set_log_segment AS g
  JOIN lifeswitch_training.training_set_log AS l
    ON l.training_set_log_id = g.training_set_log_id
  WHERE l.training_session_id = v_training_id
    AND g.owner_user_id = v_owner
    AND g.load_unit = 'lb'
    AND g.volume IN (100, 30)
    AND g.snapshot_schema_version = 2;
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'Training segment volume or provenance was not derived correctly';
  END IF;

  v_rejected := false;
  BEGIN
    PERFORM lifeswitch_training.create_training_session(
      v_training_intent || jsonb_build_object('name', 'Conflicting replay'),
      'writer-training-create-1'
    );
  EXCEPTION WHEN unique_violation THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'conflicting Training idempotency replay was accepted';
  END IF;

  v_rejected := false;
  BEGIN
    PERFORM lifeswitch_training.create_training_session(
      jsonb_build_object(
        'day', '2026-07-21',
        'name', 'Cross-account exercise test',
        'load_unit', 'lb',
        'sets', jsonb_build_array(
          jsonb_build_object(
            'exercise_id', 'other-owner-exercise',
            'exercise_sort_order', 1,
            'set_index', 1,
            'weight', 20,
            'reps', 10
          )
        )
      ),
      'writer-cross-owner-exercise'
    );
  EXCEPTION WHEN insufficient_privilege THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'Training writer accepted another owner exercise';
  END IF;

  v_training_correction := jsonb_set(
    jsonb_set(
      v_training_intent,
      '{name}',
      to_jsonb('Writer Training Session Corrected'::text)
    ),
    '{sets,0,segments,1,reps}',
    to_jsonb(4)
  );
  v_training_replacement_id :=
    lifeswitch_training.correct_training_session(
      v_training_id,
      v_training_correction,
      'writer-training-correct-1'
    );

  SELECT count(*) INTO v_count
  FROM lifeswitch_training.training_session_current_v
  WHERE owner_user_id = v_owner
    AND training_session_id = v_training_replacement_id;
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'corrected Training observation is not the current leaf';
  END IF;

  SELECT count(*) INTO v_count
  FROM lifeswitch_training.training_session_current_v
  WHERE training_session_id = v_training_id;
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'superseded Training observation remains current';
  END IF;

  SELECT count(*) INTO v_count
  FROM lifeswitch_training.training_set_log
  WHERE training_session_id = v_training_id;
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'Training correction mutated or removed original children';
  END IF;

  v_rejected := false;
  BEGIN
    UPDATE lifeswitch_training.training_set_log
    SET reps = reps + 1
    WHERE training_session_id = v_training_replacement_id;
  EXCEPTION WHEN insufficient_privilege THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'brains_app retained direct Training child updates';
  END IF;

  PERFORM set_config('app.user_id', v_other_owner::text, true);
  v_rejected := false;
  BEGIN
    PERFORM lifeswitch_training.correct_training_session(
      v_training_replacement_id,
      v_training_correction,
      'writer-cross-owner-correction'
    );
  EXCEPTION WHEN no_data_found THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'cross-account Training correction was accepted';
  END IF;
  PERFORM set_config('app.user_id', v_owner::text, true);

  PERFORM lifeswitch_training.void_training_session(
    v_training_replacement_id,
    'writer runtime test'
  );
  SELECT count(*) INTO v_count
  FROM lifeswitch_training.training_session_current_v
  WHERE owner_user_id = v_owner;
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'voided Training observation remains current';
  END IF;

  SELECT count(*) INTO v_count
  FROM lifeswitch_training.training_observation_event
  WHERE observation_type = 'training_session'
    AND owner_user_id = v_owner
    AND event_type IN ('created', 'corrected', 'voided');
  IF v_count <> 3 THEN
    RAISE EXCEPTION 'Training create/correct/void event chain is incomplete';
  END IF;

  v_conditioning_intent := jsonb_build_object(
    'day', '2026-07-21',
    'my_conditioning_prescription_id',
      'cccccccc-cccc-4ccc-8ccc-ccccccccccc1',
    'name', 'Writer Conditioning Session',
    'category', 'low_intensity_cardio',
    'modality', 'walking',
    'duration_min', 20,
    'intensity', 'easy',
    'distance_value', 1.25,
    'distance_unit', 'mi',
    'heart_rate_avg', 105,
    'dose_type', 'distance',
    'dose_config', jsonb_build_object('target', 1.25, 'unit', 'mi')
  );

  v_conditioning_id := lifeswitch_training.create_conditioning_session(
    v_conditioning_intent,
    'writer-conditioning-create-1'
  );
  v_conditioning_replay_id :=
    lifeswitch_training.create_conditioning_session(
      v_conditioning_intent,
      'writer-conditioning-create-1'
    );
  IF v_conditioning_replay_id IS DISTINCT FROM v_conditioning_id THEN
    RAISE EXCEPTION 'exact conditioning idempotency replay returned a different observation';
  END IF;

  SELECT count(*) INTO v_count
  FROM lifeswitch_training.conditioning_session_log
  WHERE conditioning_session_log_id = v_conditioning_id
    AND owner_user_id = v_owner
    AND recorded_by_user_id = v_owner
    AND distance_value = 1.25
    AND distance_unit = 'mi'
    AND distance = '1.25 mi'
    AND snapshot_schema_version = 2
    AND snapshot_quality = 'captured';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'conditioning writer failed typed distance or provenance capture';
  END IF;

  v_rejected := false;
  BEGIN
    PERFORM lifeswitch_training.create_conditioning_session(
      v_conditioning_intent || jsonb_build_object('duration_min', 25),
      'writer-conditioning-create-1'
    );
  EXCEPTION WHEN unique_violation THEN
    v_rejected := true;
  END;
  IF NOT v_rejected THEN
    RAISE EXCEPTION 'conflicting conditioning idempotency replay was accepted';
  END IF;

  v_conditioning_correction := jsonb_set(
    v_conditioning_intent,
    '{duration_min}',
    to_jsonb(25)
  );
  v_conditioning_replacement_id :=
    lifeswitch_training.correct_conditioning_session(
      v_conditioning_id,
      v_conditioning_correction,
      'writer-conditioning-correct-1'
    );

  SELECT count(*) INTO v_count
  FROM lifeswitch_training.conditioning_session_current_v
  WHERE conditioning_session_log_id = v_conditioning_replacement_id;
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'corrected conditioning observation is not the current leaf';
  END IF;

  PERFORM lifeswitch_training.void_conditioning_session(
    v_conditioning_replacement_id,
    'writer runtime test'
  );
  SELECT count(*) INTO v_count
  FROM lifeswitch_training.conditioning_session_current_v
  WHERE owner_user_id = v_owner;
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'voided conditioning observation remains current';
  END IF;

  SELECT count(*) INTO v_count
  FROM lifeswitch_training.training_observation_event
  WHERE observation_type = 'conditioning_session'
    AND owner_user_id = v_owner
    AND event_type IN ('created', 'corrected', 'voided');
  IF v_count <> 3 THEN
    RAISE EXCEPTION 'conditioning create/correct/void event chain is incomplete';
  END IF;

  RAISE NOTICE 'training writer API runtime checks passed';
END;
$test$;

RESET ROLE;
ROLLBACK;
