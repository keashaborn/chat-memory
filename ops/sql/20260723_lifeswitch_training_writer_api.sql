BEGIN;

-- INTENT-ONLY TRAINING WRITER CUTOVER
--
-- The application supplies authenticated intent. PostgreSQL resolves owner,
-- exercise role, display name, units, derived volume, provenance, correction
-- links, and immutable child rows. Direct observation-table writes remain
-- unavailable to the application role.

DO $preflight$
DECLARE
  v_owner constant text := 'lifeswitch_training_observation_owner';
  v_role pg_roles%ROWTYPE;
  v_relation regclass;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname = current_user AND rolsuper
  ) THEN
    RAISE EXCEPTION
      'training writer API migration must be run by a PostgreSQL superuser';
  END IF;

  SELECT * INTO v_role FROM pg_roles WHERE rolname = v_owner;
  IF NOT FOUND THEN
    RAISE EXCEPTION
      'protected Training observation owner role is missing; run enforcement first';
  END IF;
  IF v_role.rolcanlogin OR v_role.rolsuper OR v_role.rolcreatedb
     OR v_role.rolcreaterole OR v_role.rolinherit OR v_role.rolreplication
     OR v_role.rolbypassrls THEN
    RAISE EXCEPTION
      'protected Training observation owner role has unsafe attributes';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brains_app')
     AND pg_has_role('brains_app', v_owner, 'member') THEN
    RAISE EXCEPTION
      'brains_app must not be a member of the protected Training owner role';
  END IF;

  FOREACH v_relation IN ARRAY ARRAY[
    'lifeswitch_training.training_session'::regclass,
    'lifeswitch_training.training_set_log'::regclass,
    'lifeswitch_training.training_set_log_segment'::regclass,
    'lifeswitch_training.conditioning_session_log'::regclass
  ]
  LOOP
    IF pg_get_userbyid(
         (SELECT relowner FROM pg_class WHERE oid = v_relation)
       ) IS DISTINCT FROM v_owner THEN
      RAISE EXCEPTION '% is not owned by the protected Training role', v_relation;
    END IF;
  END LOOP;

  IF to_regprocedure(
       'lifeswitch_training.tg_enforce_training_parent_immutable()'
     ) IS NULL
     OR to_regprocedure(
       'lifeswitch_training.tg_enforce_training_child_immutable()'
     ) IS NULL THEN
    RAISE EXCEPTION
      'training writer API requires the enforcement migration';
  END IF;
END;
$preflight$;

CREATE OR REPLACE FUNCTION lifeswitch_training.current_app_user_id()
RETURNS uuid
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  v_raw text;
  v_user_id uuid;
BEGIN
  v_raw := current_setting('app.user_id', true);
  IF nullif(btrim(v_raw), '') IS NULL THEN
    RAISE EXCEPTION USING
      ERRCODE = '28000',
      MESSAGE = 'transaction-local app.user_id is required';
  END IF;
  BEGIN
    v_user_id := v_raw::uuid;
  EXCEPTION WHEN invalid_text_representation THEN
    RAISE EXCEPTION USING
      ERRCODE = '28000',
      MESSAGE = 'transaction-local app.user_id must be a UUID';
  END;
  RETURN v_user_id;
END;
$function$;

CREATE OR REPLACE FUNCTION lifeswitch_training._write_training_session(
  p_owner_user_id uuid,
  p_actor_user_id uuid,
  p_intent jsonb,
  p_idempotency_key text,
  p_supersedes_training_session_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  v_idempotency_key text;
  v_existing lifeswitch_training.training_session%ROWTYPE;
  v_session_id uuid;
  v_day date;
  v_workout_template_id uuid;
  v_workout_role text;
  v_template jsonb;
  v_name text;
  v_notes text;
  v_started_at timestamptz;
  v_finished_at timestamptz;
  v_load_unit text;
  v_set jsonb;
  v_set_ordinality bigint;
  v_set_id uuid;
  v_exercise jsonb;
  v_exercise_id text;
  v_role text;
  v_sort_order integer;
  v_set_index integer;
  v_set_type text;
  v_weight numeric;
  v_reps integer;
  v_volume numeric;
  v_segments jsonb;
  v_segment jsonb;
  v_segment_ordinality bigint;
  v_segment_index integer;
  v_segment_weight numeric;
  v_segment_reps integer;
BEGIN
  IF p_owner_user_id IS NULL OR p_actor_user_id IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'owner and actor are required';
  END IF;
  IF p_intent IS NULL OR jsonb_typeof(p_intent) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training intent must be a JSON object';
  END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_object_keys(p_intent) AS k(key_name)
    WHERE key_name <> ALL (ARRAY[
      'day', 'workout_template_id', 'name', 'notes', 'started_at',
      'finished_at', 'load_unit', 'sets'
    ])
  ) THEN
    RAISE EXCEPTION USING
      ERRCODE = '22023',
      MESSAGE = 'training intent contains an unknown or reserved field';
  END IF;

  v_idempotency_key := nullif(btrim(p_idempotency_key), '');
  IF v_idempotency_key IS NULL OR length(v_idempotency_key) > 128 THEN
    RAISE EXCEPTION USING
      ERRCODE = '22023',
      MESSAGE = 'training idempotency key must contain 1 to 128 characters';
  END IF;

  SELECT * INTO v_existing
  FROM lifeswitch_training.training_session
  WHERE owner_user_id = p_owner_user_id
    AND idempotency_key = v_idempotency_key;
  IF FOUND THEN
    IF v_existing.source_snapshot->'intent' = p_intent
       AND v_existing.supersedes_training_session_id IS NOT DISTINCT FROM
           p_supersedes_training_session_id THEN
      RETURN v_existing.training_session_id;
    END IF;
    RAISE EXCEPTION USING
      ERRCODE = '23505',
      MESSAGE = 'training idempotency key was already used for different intent';
  END IF;

  BEGIN
    v_day := (p_intent->>'day')::date;
    v_started_at := nullif(p_intent->>'started_at', '')::timestamptz;
    v_finished_at := nullif(p_intent->>'finished_at', '')::timestamptz;
    v_workout_template_id := nullif(p_intent->>'workout_template_id', '')::uuid;
  EXCEPTION WHEN invalid_text_representation OR datetime_field_overflow THEN
    RAISE EXCEPTION USING
      ERRCODE = '22023',
      MESSAGE = 'training day, timestamps, or workout template id are invalid';
  END;

  IF v_day IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training day is required';
  END IF;
  IF v_started_at IS NOT NULL AND v_finished_at IS NOT NULL
     AND v_finished_at < v_started_at THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training finish time precedes start time';
  END IF;

  v_load_unit := lower(nullif(btrim(p_intent->>'load_unit'), ''));
  IF v_load_unit NOT IN ('lb', 'kg') THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training load_unit must be lb or kg';
  END IF;

  IF v_workout_template_id IS NOT NULL THEN
    SELECT to_jsonb(w) INTO v_template
    FROM lifeswitch_training.workout_template AS w
    WHERE w.workout_template_id = v_workout_template_id
      AND w.owner_user_id = p_owner_user_id
      AND w.is_active = true;
    IF v_template IS NULL THEN
      RAISE EXCEPTION USING
        ERRCODE = '42501',
        MESSAGE = 'workout template is unavailable to the authenticated owner';
    END IF;
    v_workout_role := nullif(v_template->>'workout_role', '');
    IF v_workout_role NOT IN ('strength', 'rehab') THEN
      RAISE EXCEPTION USING
        ERRCODE = '22023',
        MESSAGE = 'workout template must be classified before completing a session';
    END IF;
  END IF;

  v_name := coalesce(
    nullif(btrim(p_intent->>'name'), ''),
    nullif(btrim(v_template->>'name'), '')
  );
  v_notes := nullif(btrim(p_intent->>'notes'), '');
  IF v_name IS NULL OR length(v_name) > 200 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training name must contain 1 to 200 characters';
  END IF;
  IF v_notes IS NOT NULL AND length(v_notes) > 2000 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training notes must not exceed 2000 characters';
  END IF;
  IF jsonb_typeof(p_intent->'sets') IS DISTINCT FROM 'array' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training sets must be a JSON array';
  END IF;
  IF jsonb_array_length(p_intent->'sets') = 0 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training intent requires at least one set';
  END IF;

  BEGIN
    INSERT INTO lifeswitch_training.training_session (
      owner_user_id, recorded_by_user_id, day, workout_template_id,
      workout_role_snapshot,
      name, notes, started_at, finished_at, is_active, idempotency_key,
      source_snapshot, snapshot_schema_version, snapshot_quality,
      supersedes_training_session_id
    ) VALUES (
      p_owner_user_id, p_actor_user_id, v_day, v_workout_template_id,
      v_workout_role,
      v_name, v_notes, v_started_at, v_finished_at, true, v_idempotency_key,
      jsonb_build_object(
        'kind', 'training_session',
        'intent', p_intent,
        'workout_template', v_template
      ),
      2, 'captured', p_supersedes_training_session_id
    )
    RETURNING training_session_id INTO v_session_id;
  EXCEPTION WHEN unique_violation THEN
    SELECT * INTO v_existing
    FROM lifeswitch_training.training_session
    WHERE owner_user_id = p_owner_user_id
      AND idempotency_key = v_idempotency_key;
    IF FOUND
       AND v_existing.source_snapshot->'intent' = p_intent
       AND v_existing.supersedes_training_session_id IS NOT DISTINCT FROM
           p_supersedes_training_session_id THEN
      RETURN v_existing.training_session_id;
    END IF;
    RAISE;
  END;

  FOR v_set, v_set_ordinality IN
    SELECT value, ordinality
    FROM jsonb_array_elements(p_intent->'sets') WITH ORDINALITY
  LOOP
    IF jsonb_typeof(v_set) IS DISTINCT FROM 'object'
       OR EXISTS (
         SELECT 1 FROM jsonb_object_keys(v_set) AS k(key_name)
         WHERE key_name <> ALL (ARRAY[
           'exercise_id', 'exercise_sort_order', 'set_index', 'set_type',
           'weight', 'reps', 'flags', 'notes', 'segments'
         ])
       ) THEN
      RAISE EXCEPTION USING
        ERRCODE = '22023',
        MESSAGE = 'training set contains an unknown or reserved field';
    END IF;

    v_exercise_id := nullif(btrim(v_set->>'exercise_id'), '');
    BEGIN
      v_sort_order := (v_set->>'exercise_sort_order')::integer;
      v_set_index := (v_set->>'set_index')::integer;
      v_weight := (v_set->>'weight')::numeric;
      v_reps := (v_set->>'reps')::integer;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training set numeric values are invalid';
    END;
    v_set_type := coalesce(nullif(btrim(v_set->>'set_type'), ''), 'straight');
    IF v_exercise_id IS NULL OR v_sort_order IS NULL OR v_sort_order < 0
       OR v_set_index IS NULL OR v_set_index < 1
       OR v_weight IS NULL OR v_weight < 0
       OR v_reps IS NULL OR v_reps < 0
       OR length(v_set_type) > 64 THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training set contract is incomplete or invalid';
    END IF;
    IF length(coalesce(v_set->>'flags', '')) > 500
       OR length(coalesce(v_set->>'notes', '')) > 1000 THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training set text exceeds its limit';
    END IF;

    SELECT to_jsonb(e), e.exercise_role INTO v_exercise, v_role
    FROM lifeswitch_training.my_exercise AS e
    WHERE e.owner_user_id = p_owner_user_id
      AND e.exercise_id = v_exercise_id
      AND e.is_active = true;
    IF v_exercise IS NULL THEN
      RAISE EXCEPTION USING
        ERRCODE = '42501',
        MESSAGE = 'exercise is unavailable to the authenticated owner';
    END IF;

    v_segments := coalesce(v_set->'segments', '[]'::jsonb);
    IF jsonb_typeof(v_segments) IS DISTINCT FROM 'array' THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training set segments must be an array';
    END IF;

    v_volume := 0;
    IF jsonb_array_length(v_segments) = 0 THEN
      v_volume := v_weight * v_reps;
    ELSE
      FOR v_segment, v_segment_ordinality IN
        SELECT value, ordinality
        FROM jsonb_array_elements(v_segments) WITH ORDINALITY
      LOOP
        IF jsonb_typeof(v_segment) IS DISTINCT FROM 'object'
           OR EXISTS (
             SELECT 1 FROM jsonb_object_keys(v_segment) AS k(key_name)
             WHERE key_name <> ALL (ARRAY[
               'segment_index', 'label', 'weight', 'reps', 'notes'
             ])
           ) THEN
          RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training segment contains an unknown field';
        END IF;
        BEGIN
          v_segment_index := (v_segment->>'segment_index')::integer;
          v_segment_weight := (v_segment->>'weight')::numeric;
          v_segment_reps := (v_segment->>'reps')::integer;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
          RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training segment numeric values are invalid';
        END;
        IF v_segment_index IS NULL OR v_segment_index < 1
           OR v_segment_weight IS NULL OR v_segment_weight < 0
           OR v_segment_reps IS NULL OR v_segment_reps < 0
           OR length(coalesce(v_segment->>'label', '')) > 200
           OR length(coalesce(v_segment->>'notes', '')) > 1000 THEN
          RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training segment contract is incomplete or invalid';
        END IF;
        v_volume := v_volume + (v_segment_weight * v_segment_reps);
      END LOOP;
      IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(v_segments) AS x(value)
        GROUP BY (value->>'segment_index')::integer
        HAVING count(*) > 1
      ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'training segment indexes must be unique within a set';
      END IF;
    END IF;

    INSERT INTO lifeswitch_training.training_set_log (
      training_session_id, owner_user_id, workout_template_id,
      exercise_id, exercise_name, exercise_sort_order, set_index,
      weight, reps, volume, flags, notes, is_active, set_type,
      exercise_role_snapshot, capture_role, load_unit, source_snapshot,
      snapshot_schema_version, snapshot_quality
    ) VALUES (
      v_session_id, p_owner_user_id, v_workout_template_id,
      v_exercise_id, v_exercise->>'display_name', v_sort_order, v_set_index,
      v_weight, v_reps, v_volume, nullif(v_set->>'flags', ''),
      nullif(v_set->>'notes', ''), true, v_set_type,
      v_role, v_role, v_load_unit,
      jsonb_build_object(
        'kind', 'training_set', 'intent', v_set,
        'exercise', v_exercise, 'capture_role', v_role,
        'load_unit', v_load_unit, 'derived_volume', v_volume
      ),
      2, 'captured'
    ) RETURNING training_set_log_id INTO v_set_id;

    FOR v_segment, v_segment_ordinality IN
      SELECT value, ordinality
      FROM jsonb_array_elements(v_segments) WITH ORDINALITY
    LOOP
      v_segment_index := (v_segment->>'segment_index')::integer;
      v_segment_weight := (v_segment->>'weight')::numeric;
      v_segment_reps := (v_segment->>'reps')::integer;
      INSERT INTO lifeswitch_training.training_set_log_segment (
        training_set_log_id, owner_user_id, segment_index, label,
        weight, reps, volume, notes, load_unit, source_snapshot,
        snapshot_schema_version, snapshot_quality
      ) VALUES (
        v_set_id, p_owner_user_id, v_segment_index,
        nullif(v_segment->>'label', ''), v_segment_weight, v_segment_reps,
        v_segment_weight * v_segment_reps, nullif(v_segment->>'notes', ''),
        v_load_unit,
        jsonb_build_object(
          'kind', 'training_set_segment', 'intent', v_segment,
          'load_unit', v_load_unit,
          'derived_volume', v_segment_weight * v_segment_reps
        ),
        2, 'captured'
      );
    END LOOP;
  END LOOP;

  IF EXISTS (
    SELECT 1 FROM lifeswitch_training.training_set_log
    WHERE training_session_id = v_session_id
    GROUP BY exercise_id, set_index
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION USING
      ERRCODE = '22023',
      MESSAGE = 'set indexes must be unique per exercise within a session';
  END IF;

  RETURN v_session_id;
END;
$function$;

CREATE OR REPLACE FUNCTION lifeswitch_training.create_training_session(
  p_intent jsonb,
  p_idempotency_key text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  v_actor uuid;
BEGIN
  v_actor := lifeswitch_training.current_app_user_id();
  RETURN lifeswitch_training._write_training_session(
    v_actor, v_actor, p_intent, p_idempotency_key, NULL
  );
END;
$function$;

CREATE OR REPLACE FUNCTION lifeswitch_training.correct_training_session(
  p_training_session_id uuid,
  p_intent jsonb,
  p_idempotency_key text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  v_actor uuid;
  v_original uuid;
  v_existing lifeswitch_training.training_session%ROWTYPE;
  v_key text;
BEGIN
  v_actor := lifeswitch_training.current_app_user_id();
  v_key := nullif(btrim(p_idempotency_key), '');

  SELECT * INTO v_existing
  FROM lifeswitch_training.training_session
  WHERE owner_user_id = v_actor AND idempotency_key = v_key;
  IF FOUND THEN
    IF v_existing.source_snapshot->'intent' = p_intent
       AND v_existing.supersedes_training_session_id IS NOT DISTINCT FROM
           p_training_session_id THEN
      RETURN v_existing.training_session_id;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23505', MESSAGE = 'training idempotency key conflicts';
  END IF;

  SELECT s.training_session_id INTO v_original
  FROM lifeswitch_training.training_session AS s
  WHERE s.training_session_id = p_training_session_id
    AND s.owner_user_id = v_actor
    AND s.voided_at IS NULL
    AND s.is_active = true
    AND NOT EXISTS (
      SELECT 1 FROM lifeswitch_training.training_session AS replacement
      WHERE replacement.supersedes_training_session_id = s.training_session_id
    )
  FOR UPDATE;
  IF v_original IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'current training session was not found';
  END IF;

  RETURN lifeswitch_training._write_training_session(
    v_actor, v_actor, p_intent, p_idempotency_key, v_original
  );
END;
$function$;

CREATE OR REPLACE FUNCTION lifeswitch_training.void_training_session(
  p_training_session_id uuid,
  p_reason text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  v_actor uuid;
  v_reason text;
  v_result uuid;
BEGIN
  v_actor := lifeswitch_training.current_app_user_id();
  v_reason := nullif(btrim(p_reason), '');
  IF v_reason IS NULL OR length(v_reason) > 1000 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'void reason must contain 1 to 1000 characters';
  END IF;

  UPDATE lifeswitch_training.training_session AS s
  SET voided_at = clock_timestamp(), voided_by_user_id = v_actor,
      void_reason = v_reason, is_active = false, updated_at = clock_timestamp()
  WHERE s.training_session_id = p_training_session_id
    AND s.owner_user_id = v_actor
    AND s.voided_at IS NULL
    AND s.is_active = true
    AND NOT EXISTS (
      SELECT 1 FROM lifeswitch_training.training_session AS replacement
      WHERE replacement.supersedes_training_session_id = s.training_session_id
    )
  RETURNING s.training_session_id INTO v_result;
  IF v_result IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'current training session was not found';
  END IF;
  RETURN v_result;
END;
$function$;

CREATE OR REPLACE FUNCTION lifeswitch_training._write_conditioning_session(
  p_owner_user_id uuid,
  p_actor_user_id uuid,
  p_intent jsonb,
  p_idempotency_key text,
  p_supersedes_conditioning_session_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  v_key text;
  v_existing lifeswitch_training.conditioning_session_log%ROWTYPE;
  v_id uuid;
  v_prescription_id uuid;
  v_prescription jsonb;
  v_day date;
  v_name text;
  v_category text;
  v_modality text;
  v_duration numeric;
  v_intensity text;
  v_distance_value numeric;
  v_distance_unit text;
  v_distance text;
  v_heart_rate numeric;
  v_recovery_impact text;
  v_notes text;
  v_dose_type text;
  v_dose_config jsonb;
BEGIN
  IF p_owner_user_id IS NULL OR p_actor_user_id IS NULL
     OR p_intent IS NULL
     OR jsonb_typeof(p_intent) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'conditioning owner, actor, and object intent are required';
  END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_object_keys(p_intent) AS k(key_name)
    WHERE key_name <> ALL (ARRAY[
      'day', 'my_conditioning_prescription_id', 'name', 'category',
      'modality', 'duration_min', 'intensity', 'distance_value',
      'distance_unit', 'heart_rate_avg', 'recovery_impact', 'notes',
      'dose_type', 'dose_config'
    ])
  ) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'conditioning intent contains an unknown or reserved field';
  END IF;

  v_key := nullif(btrim(p_idempotency_key), '');
  IF v_key IS NULL OR length(v_key) > 128 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'conditioning idempotency key must contain 1 to 128 characters';
  END IF;
  SELECT * INTO v_existing
  FROM lifeswitch_training.conditioning_session_log
  WHERE owner_user_id = p_owner_user_id AND idempotency_key = v_key;
  IF FOUND THEN
    IF v_existing.source_snapshot->'intent' = p_intent
       AND v_existing.supersedes_conditioning_session_id IS NOT DISTINCT FROM
           p_supersedes_conditioning_session_id THEN
      RETURN v_existing.conditioning_session_log_id;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23505', MESSAGE = 'conditioning idempotency key conflicts';
  END IF;

  BEGIN
    v_day := (p_intent->>'day')::date;
    v_prescription_id := nullif(p_intent->>'my_conditioning_prescription_id', '')::uuid;
    v_duration := coalesce((p_intent->>'duration_min')::numeric, 0);
    v_distance_value := nullif(p_intent->>'distance_value', '')::numeric;
    v_heart_rate := nullif(p_intent->>'heart_rate_avg', '')::numeric;
  EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR datetime_field_overflow THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'conditioning date, id, or numeric value is invalid';
  END;
  IF v_day IS NULL OR v_duration < 0 OR v_distance_value < 0 OR v_heart_rate < 0 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'conditioning values are incomplete or negative';
  END IF;

  IF v_prescription_id IS NOT NULL THEN
    SELECT to_jsonb(p) INTO v_prescription
    FROM lifeswitch_training.my_conditioning_prescription AS p
    WHERE p.my_conditioning_prescription_id = v_prescription_id
      AND p.owner_user_id = p_owner_user_id
      AND p.is_active = true;
    IF v_prescription IS NULL THEN
      RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'conditioning prescription is unavailable to the authenticated owner';
    END IF;
  END IF;

  v_name := coalesce(nullif(btrim(p_intent->>'name'), ''), nullif(btrim(v_prescription->>'name'), ''));
  v_category := coalesce(nullif(btrim(p_intent->>'category'), ''), v_prescription->>'category', '');
  v_modality := coalesce(nullif(btrim(p_intent->>'modality'), ''), v_prescription->>'modality', '');
  v_intensity := coalesce(nullif(btrim(p_intent->>'intensity'), ''), v_prescription->>'target_intensity', '');
  v_recovery_impact := coalesce(p_intent->>'recovery_impact', '');
  v_notes := coalesce(p_intent->>'notes', '');
  v_distance_unit := lower(nullif(btrim(p_intent->>'distance_unit'), ''));
  v_dose_type := coalesce(nullif(btrim(p_intent->>'dose_type'), ''), 'open');
  v_dose_config := coalesce(p_intent->'dose_config', '{}'::jsonb);

  IF v_name IS NULL OR length(v_name) > 200
     OR length(v_category) > 200 OR length(v_modality) > 200
     OR length(v_intensity) > 500 OR length(v_recovery_impact) > 1000
     OR length(v_notes) > 2000 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'conditioning text is missing or exceeds its limit';
  END IF;
  IF (v_distance_value IS NULL) <> (v_distance_unit IS NULL)
     OR (v_distance_unit IS NOT NULL AND v_distance_unit NOT IN ('mi', 'km', 'm', 'yd', 'ft')) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'conditioning distance requires a valid value and unit pair';
  END IF;
  IF v_dose_type NOT IN ('open', 'time', 'distance', 'rounds', 'intervals', 'laps', 'repetitions', 'loaded_carry')
     OR jsonb_typeof(v_dose_config) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'conditioning dose contract is invalid';
  END IF;
  v_distance := CASE WHEN v_distance_value IS NULL THEN '' ELSE v_distance_value::text || ' ' || v_distance_unit END;

  BEGIN
    INSERT INTO lifeswitch_training.conditioning_session_log (
      owner_user_id, recorded_by_user_id, my_conditioning_prescription_id,
      day, name, category, modality, duration_min, intensity, distance,
      heart_rate_avg, recovery_impact, notes, is_active, dose_type,
      dose_config, idempotency_key, source_snapshot, snapshot_schema_version,
      snapshot_quality, supersedes_conditioning_session_id,
      distance_value, distance_unit
    ) VALUES (
      p_owner_user_id, p_actor_user_id, v_prescription_id, v_day, v_name,
      v_category, v_modality, v_duration, v_intensity, v_distance,
      v_heart_rate, v_recovery_impact, v_notes, true, v_dose_type,
      v_dose_config, v_key,
      jsonb_build_object(
        'kind', 'conditioning_session', 'intent', p_intent,
        'prescription', v_prescription
      ),
      2, 'captured', p_supersedes_conditioning_session_id,
      v_distance_value, v_distance_unit
    ) RETURNING conditioning_session_log_id INTO v_id;
  EXCEPTION WHEN unique_violation THEN
    SELECT * INTO v_existing
    FROM lifeswitch_training.conditioning_session_log
    WHERE owner_user_id = p_owner_user_id AND idempotency_key = v_key;
    IF FOUND AND v_existing.source_snapshot->'intent' = p_intent
       AND v_existing.supersedes_conditioning_session_id IS NOT DISTINCT FROM
           p_supersedes_conditioning_session_id THEN
      RETURN v_existing.conditioning_session_log_id;
    END IF;
    RAISE;
  END;
  RETURN v_id;
END;
$function$;

CREATE OR REPLACE FUNCTION lifeswitch_training.create_conditioning_session(
  p_intent jsonb,
  p_idempotency_key text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE v_actor uuid;
BEGIN
  v_actor := lifeswitch_training.current_app_user_id();
  RETURN lifeswitch_training._write_conditioning_session(
    v_actor, v_actor, p_intent, p_idempotency_key, NULL
  );
END;
$function$;

CREATE OR REPLACE FUNCTION lifeswitch_training.correct_conditioning_session(
  p_conditioning_session_log_id uuid,
  p_intent jsonb,
  p_idempotency_key text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  v_actor uuid;
  v_original uuid;
  v_existing lifeswitch_training.conditioning_session_log%ROWTYPE;
  v_key text;
BEGIN
  v_actor := lifeswitch_training.current_app_user_id();
  v_key := nullif(btrim(p_idempotency_key), '');
  SELECT * INTO v_existing
  FROM lifeswitch_training.conditioning_session_log
  WHERE owner_user_id = v_actor AND idempotency_key = v_key;
  IF FOUND THEN
    IF v_existing.source_snapshot->'intent' = p_intent
       AND v_existing.supersedes_conditioning_session_id IS NOT DISTINCT FROM
           p_conditioning_session_log_id THEN
      RETURN v_existing.conditioning_session_log_id;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23505', MESSAGE = 'conditioning idempotency key conflicts';
  END IF;

  SELECT c.conditioning_session_log_id INTO v_original
  FROM lifeswitch_training.conditioning_session_log AS c
  WHERE c.conditioning_session_log_id = p_conditioning_session_log_id
    AND c.owner_user_id = v_actor
    AND c.voided_at IS NULL AND c.is_active = true
    AND NOT EXISTS (
      SELECT 1 FROM lifeswitch_training.conditioning_session_log AS replacement
      WHERE replacement.supersedes_conditioning_session_id = c.conditioning_session_log_id
    )
  FOR UPDATE;
  IF v_original IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'current conditioning session was not found';
  END IF;
  RETURN lifeswitch_training._write_conditioning_session(
    v_actor, v_actor, p_intent, p_idempotency_key, v_original
  );
END;
$function$;

CREATE OR REPLACE FUNCTION lifeswitch_training.void_conditioning_session(
  p_conditioning_session_log_id uuid,
  p_reason text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  v_actor uuid;
  v_reason text;
  v_result uuid;
BEGIN
  v_actor := lifeswitch_training.current_app_user_id();
  v_reason := nullif(btrim(p_reason), '');
  IF v_reason IS NULL OR length(v_reason) > 1000 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'void reason must contain 1 to 1000 characters';
  END IF;
  UPDATE lifeswitch_training.conditioning_session_log AS c
  SET voided_at = clock_timestamp(), voided_by_user_id = v_actor,
      void_reason = v_reason, is_active = false, updated_at = clock_timestamp()
  WHERE c.conditioning_session_log_id = p_conditioning_session_log_id
    AND c.owner_user_id = v_actor
    AND c.voided_at IS NULL AND c.is_active = true
    AND NOT EXISTS (
      SELECT 1 FROM lifeswitch_training.conditioning_session_log AS replacement
      WHERE replacement.supersedes_conditioning_session_id = c.conditioning_session_log_id
    )
  RETURNING c.conditioning_session_log_id INTO v_result;
  IF v_result IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'current conditioning session was not found';
  END IF;
  RETURN v_result;
END;
$function$;

ALTER FUNCTION lifeswitch_training.current_app_user_id()
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training._write_training_session(uuid, uuid, jsonb, text, uuid)
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training.create_training_session(jsonb, text)
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training.correct_training_session(uuid, jsonb, text)
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training.void_training_session(uuid, text)
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training._write_conditioning_session(uuid, uuid, jsonb, text, uuid)
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training.create_conditioning_session(jsonb, text)
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training.correct_conditioning_session(uuid, jsonb, text)
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training.void_conditioning_session(uuid, text)
  OWNER TO lifeswitch_training_observation_owner;

REVOKE ALL ON FUNCTION lifeswitch_training.current_app_user_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION lifeswitch_training._write_training_session(uuid, uuid, jsonb, text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION lifeswitch_training.create_training_session(jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION lifeswitch_training.correct_training_session(uuid, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION lifeswitch_training.void_training_session(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION lifeswitch_training._write_conditioning_session(uuid, uuid, jsonb, text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION lifeswitch_training.create_conditioning_session(jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION lifeswitch_training.correct_conditioning_session(uuid, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION lifeswitch_training.void_conditioning_session(uuid, text) FROM PUBLIC;

DO $writer_acl$
DECLARE
  v_role_name text;
  v_function regprocedure;
BEGIN
  FOREACH v_role_name IN ARRAY ARRAY['anon', 'authenticated', 'brains_app']
  LOOP
    CONTINUE WHEN NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role_name);
    FOREACH v_function IN ARRAY ARRAY[
      'lifeswitch_training.current_app_user_id()'::regprocedure,
      'lifeswitch_training._write_training_session(uuid,uuid,jsonb,text,uuid)'::regprocedure,
      'lifeswitch_training.create_training_session(jsonb,text)'::regprocedure,
      'lifeswitch_training.correct_training_session(uuid,jsonb,text)'::regprocedure,
      'lifeswitch_training.void_training_session(uuid,text)'::regprocedure,
      'lifeswitch_training._write_conditioning_session(uuid,uuid,jsonb,text,uuid)'::regprocedure,
      'lifeswitch_training.create_conditioning_session(jsonb,text)'::regprocedure,
      'lifeswitch_training.correct_conditioning_session(uuid,jsonb,text)'::regprocedure,
      'lifeswitch_training.void_conditioning_session(uuid,text)'::regprocedure
    ]
    LOOP
      EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %I', v_function, v_role_name);
    END LOOP;
  END LOOP;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brains_app') THEN
    GRANT USAGE ON SCHEMA lifeswitch_training TO brains_app;
    GRANT EXECUTE ON FUNCTION lifeswitch_training.create_training_session(jsonb, text) TO brains_app;
    GRANT EXECUTE ON FUNCTION lifeswitch_training.correct_training_session(uuid, jsonb, text) TO brains_app;
    GRANT EXECUTE ON FUNCTION lifeswitch_training.void_training_session(uuid, text) TO brains_app;
    GRANT EXECUTE ON FUNCTION lifeswitch_training.create_conditioning_session(jsonb, text) TO brains_app;
    GRANT EXECUTE ON FUNCTION lifeswitch_training.correct_conditioning_session(uuid, jsonb, text) TO brains_app;
    GRANT EXECUTE ON FUNCTION lifeswitch_training.void_conditioning_session(uuid, text) TO brains_app;
  END IF;
END;
$writer_acl$;

DO $writer_postflight$
DECLARE
  v_relation regclass;
  v_protected_function regprocedure;
  v_public_function regprocedure;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brains_app') THEN
    FOREACH v_relation IN ARRAY ARRAY[
      'lifeswitch_training.training_session'::regclass,
      'lifeswitch_training.training_set_log'::regclass,
      'lifeswitch_training.training_set_log_segment'::regclass,
      'lifeswitch_training.conditioning_session_log'::regclass,
      'lifeswitch_training.training_observation_event'::regclass
    ] LOOP
      IF has_table_privilege('brains_app', v_relation, 'INSERT, UPDATE, DELETE, TRUNCATE')
         OR has_any_column_privilege('brains_app', v_relation, 'INSERT, UPDATE') THEN
        RAISE EXCEPTION 'brains_app retains raw Training write access on %', v_relation;
      END IF;
    END LOOP;

    FOREACH v_protected_function IN ARRAY ARRAY[
      'lifeswitch_training.current_app_user_id()'::regprocedure,
      'lifeswitch_training._write_training_session(uuid,uuid,jsonb,text,uuid)'::regprocedure,
      'lifeswitch_training._write_conditioning_session(uuid,uuid,jsonb,text,uuid)'::regprocedure
    ] LOOP
      IF has_function_privilege('brains_app', v_protected_function, 'EXECUTE') THEN
        RAISE EXCEPTION 'brains_app can execute protected Training helper %', v_protected_function;
      END IF;
    END LOOP;

    FOREACH v_public_function IN ARRAY ARRAY[
      'lifeswitch_training.create_training_session(jsonb,text)'::regprocedure,
      'lifeswitch_training.correct_training_session(uuid,jsonb,text)'::regprocedure,
      'lifeswitch_training.void_training_session(uuid,text)'::regprocedure,
      'lifeswitch_training.create_conditioning_session(jsonb,text)'::regprocedure,
      'lifeswitch_training.correct_conditioning_session(uuid,jsonb,text)'::regprocedure,
      'lifeswitch_training.void_conditioning_session(uuid,text)'::regprocedure
    ] LOOP
      IF NOT has_function_privilege('brains_app', v_public_function, 'EXECUTE') THEN
        RAISE EXCEPTION 'brains_app cannot execute Training writer %', v_public_function;
      END IF;
    END LOOP;
  END IF;
END;
$writer_postflight$;

COMMENT ON FUNCTION lifeswitch_training.create_training_session(jsonb, text) IS
  'Creates one immutable completed strength/rehab session aggregate from authenticated intent.';
COMMENT ON FUNCTION lifeswitch_training.correct_training_session(uuid, jsonb, text) IS
  'Creates a complete immutable replacement aggregate; never mutates the original session.';
COMMENT ON FUNCTION lifeswitch_training.void_training_session(uuid, text) IS
  'Voids only the authenticated owner current Training leaf and preserves all captured children.';
COMMENT ON FUNCTION lifeswitch_training.create_conditioning_session(jsonb, text) IS
  'Creates one immutable conditioning observation from authenticated intent.';

COMMIT;
