BEGIN;

ALTER TABLE lifeswitch_training.workout_template
  ADD COLUMN IF NOT EXISTS workout_role text;

ALTER TABLE lifeswitch_training.training_session
  ADD COLUMN IF NOT EXISTS workout_role_snapshot text;

DO $migration$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'workout_template_role_allowed'
      AND conrelid = 'lifeswitch_training.workout_template'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.workout_template
      ADD CONSTRAINT workout_template_role_allowed
      CHECK (workout_role IS NULL OR workout_role IN ('strength', 'rehab'));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'training_session_workout_role_snapshot_allowed'
      AND conrelid = 'lifeswitch_training.training_session'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_session
      ADD CONSTRAINT training_session_workout_role_snapshot_allowed
      CHECK (workout_role_snapshot IS NULL OR workout_role_snapshot IN ('strength', 'rehab'));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'workout_template_owner_id_unique'
      AND conrelid = 'lifeswitch_training.workout_template'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.workout_template
      ADD CONSTRAINT workout_template_owner_id_unique
      UNIQUE (owner_user_id, workout_template_id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'training_session_owner_id_unique'
      AND conrelid = 'lifeswitch_training.training_session'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_session
      ADD CONSTRAINT training_session_owner_id_unique
      UNIQUE (owner_user_id, training_session_id);
  END IF;
END
$migration$;

CREATE INDEX IF NOT EXISTS ix_workout_template_owner_role_active
  ON lifeswitch_training.workout_template(owner_user_id, workout_role)
  WHERE is_active = true;

CREATE TABLE IF NOT EXISTS lifeswitch_training.workout_template_role_event (
  workout_template_role_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  workout_template_id uuid NOT NULL,
  previous_role text,
  new_role text NOT NULL,
  changed_by_user_id uuid NOT NULL,
  reason text,
  idempotency_key text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT workout_template_role_event_roles_allowed CHECK (
    (previous_role IS NULL OR previous_role IN ('strength', 'rehab'))
    AND new_role IN ('strength', 'rehab')
  ),
  CONSTRAINT workout_template_role_event_owner_template_fk
    FOREIGN KEY (owner_user_id, workout_template_id)
    REFERENCES lifeswitch_training.workout_template(owner_user_id, workout_template_id)
    ON DELETE RESTRICT,
  CONSTRAINT workout_template_role_event_owner_key_unique
    UNIQUE (owner_user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS lifeswitch_training.training_session_role_event (
  training_session_role_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  training_session_id uuid NOT NULL,
  workout_template_id uuid NOT NULL,
  assigned_role text NOT NULL CHECK (assigned_role IN ('strength', 'rehab')),
  changed_by_user_id uuid NOT NULL,
  reason text,
  batch_idempotency_key text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT training_session_role_event_owner_session_fk
    FOREIGN KEY (owner_user_id, training_session_id)
    REFERENCES lifeswitch_training.training_session(owner_user_id, training_session_id)
    ON DELETE RESTRICT,
  CONSTRAINT training_session_role_event_owner_template_fk
    FOREIGN KEY (owner_user_id, workout_template_id)
    REFERENCES lifeswitch_training.workout_template(owner_user_id, workout_template_id)
    ON DELETE RESTRICT,
  CONSTRAINT training_session_role_event_session_unique UNIQUE (training_session_id),
  CONSTRAINT training_session_role_event_batch_session_unique
    UNIQUE (owner_user_id, batch_idempotency_key, training_session_id)
);

CREATE INDEX IF NOT EXISTS ix_workout_template_role_event_owner_created
  ON lifeswitch_training.workout_template_role_event(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_training_session_role_event_owner_created
  ON lifeswitch_training.training_session_role_event(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_training_session_role_event_template
  ON lifeswitch_training.training_session_role_event(owner_user_id, workout_template_id);

CREATE OR REPLACE FUNCTION lifeswitch_training.protect_workout_role_event()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
  RAISE EXCEPTION 'workout role events are append-only';
END
$function$;

DROP TRIGGER IF EXISTS trg_workout_template_role_event_append_only
  ON lifeswitch_training.workout_template_role_event;
CREATE TRIGGER trg_workout_template_role_event_append_only
BEFORE UPDATE OR DELETE ON lifeswitch_training.workout_template_role_event
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.protect_workout_role_event();

DROP TRIGGER IF EXISTS trg_training_session_role_event_append_only
  ON lifeswitch_training.training_session_role_event;
CREATE TRIGGER trg_training_session_role_event_append_only
BEFORE UPDATE OR DELETE ON lifeswitch_training.training_session_role_event
FOR EACH ROW EXECUTE FUNCTION lifeswitch_training.protect_workout_role_event();

CREATE OR REPLACE FUNCTION lifeswitch_training.set_workout_template_role(
  p_owner_user_id uuid,
  p_workout_template_id uuid,
  p_workout_role text,
  p_reason text,
  p_idempotency_key text
)
RETURNS lifeswitch_training.workout_template
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
  v_actor uuid;
  v_template lifeswitch_training.workout_template%ROWTYPE;
  v_existing lifeswitch_training.workout_template_role_event%ROWTYPE;
BEGIN
  v_actor := lifeswitch_training.current_app_user_id();
  IF v_actor IS DISTINCT FROM p_owner_user_id THEN
    RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='actor_owner_mismatch';
  END IF;
  IF p_workout_role NOT IN ('strength', 'rehab') THEN
    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='workout role must be strength or rehab';
  END IF;
  IF nullif(btrim(p_idempotency_key), '') IS NULL OR length(p_idempotency_key) > 128 THEN
    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid workout role idempotency key';
  END IF;

  SELECT * INTO v_existing
  FROM lifeswitch_training.workout_template_role_event
  WHERE owner_user_id=p_owner_user_id AND idempotency_key=p_idempotency_key;
  IF FOUND THEN
    IF v_existing.workout_template_id IS DISTINCT FROM p_workout_template_id
       OR v_existing.new_role IS DISTINCT FROM p_workout_role THEN
      RAISE EXCEPTION USING ERRCODE='23505', MESSAGE='workout role idempotency key reused';
    END IF;
    SELECT * INTO v_template FROM lifeswitch_training.workout_template
    WHERE owner_user_id=p_owner_user_id AND workout_template_id=p_workout_template_id;
    RETURN v_template;
  END IF;

  SELECT * INTO v_template
  FROM lifeswitch_training.workout_template
  WHERE owner_user_id=p_owner_user_id AND workout_template_id=p_workout_template_id
  FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE='P0002', MESSAGE='workout template not found';
  END IF;

  IF v_template.workout_role IS DISTINCT FROM p_workout_role THEN
    INSERT INTO lifeswitch_training.workout_template_role_event (
      owner_user_id, workout_template_id, previous_role, new_role,
      changed_by_user_id, reason, idempotency_key
    ) VALUES (
      p_owner_user_id, p_workout_template_id, v_template.workout_role, p_workout_role,
      v_actor, nullif(btrim(p_reason), ''), p_idempotency_key
    );
    UPDATE lifeswitch_training.workout_template
    SET workout_role=p_workout_role, updated_at=now()
    WHERE owner_user_id=p_owner_user_id AND workout_template_id=p_workout_template_id
    RETURNING * INTO v_template;
  END IF;
  RETURN v_template;
END
$function$;

CREATE OR REPLACE FUNCTION lifeswitch_training.classify_unclassified_training_sessions(
  p_owner_user_id uuid,
  p_workout_template_id uuid,
  p_workout_role text,
  p_reason text,
  p_idempotency_key text
)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
  v_actor uuid;
  v_template_role text;
  v_count integer;
BEGIN
  v_actor := lifeswitch_training.current_app_user_id();
  IF v_actor IS DISTINCT FROM p_owner_user_id THEN
    RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='actor_owner_mismatch';
  END IF;
  IF p_workout_role NOT IN ('strength', 'rehab') THEN
    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='workout role must be strength or rehab';
  END IF;
  IF nullif(btrim(p_idempotency_key), '') IS NULL OR length(p_idempotency_key) > 128 THEN
    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid historical role idempotency key';
  END IF;

  SELECT workout_role INTO v_template_role
  FROM lifeswitch_training.workout_template
  WHERE owner_user_id=p_owner_user_id AND workout_template_id=p_workout_template_id
  FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE='P0002', MESSAGE='workout template not found';
  END IF;
  IF v_template_role IS DISTINCT FROM p_workout_role THEN
    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='historical role must match the current workout template role';
  END IF;

  INSERT INTO lifeswitch_training.training_session_role_event (
    owner_user_id, training_session_id, workout_template_id, assigned_role,
    changed_by_user_id, reason, batch_idempotency_key
  )
  SELECT s.owner_user_id, s.training_session_id, p_workout_template_id, p_workout_role,
         v_actor, nullif(btrim(p_reason), ''), p_idempotency_key
  FROM lifeswitch_training.training_session s
  WHERE s.owner_user_id=p_owner_user_id
    AND s.workout_template_id=p_workout_template_id
    AND s.finished_at IS NOT NULL
    AND s.is_active=true
    AND s.workout_role_snapshot IS NULL
    AND NOT EXISTS (
      SELECT 1 FROM lifeswitch_training.training_session_role_event e
      WHERE e.training_session_id=s.training_session_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM lifeswitch_training.training_set_log l
      WHERE l.training_session_id=s.training_session_id
        AND l.is_active=true
        AND coalesce(l.capture_role, l.exercise_role_snapshot, 'unknown') IN ('strength', 'rehab')
    )
  ON CONFLICT (training_session_id) DO NOTHING;
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END
$function$;

ALTER FUNCTION lifeswitch_training.set_workout_template_role(uuid, uuid, text, text, text)
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training.classify_unclassified_training_sessions(uuid, uuid, text, text, text)
  OWNER TO lifeswitch_training_observation_owner;

REVOKE ALL ON FUNCTION lifeswitch_training.set_workout_template_role(uuid, uuid, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION lifeswitch_training.classify_unclassified_training_sessions(uuid, uuid, text, text, text) FROM PUBLIC;

GRANT SELECT, INSERT ON lifeswitch_training.workout_template_role_event
  TO lifeswitch_training_observation_owner;
GRANT SELECT, INSERT ON lifeswitch_training.training_session_role_event
  TO lifeswitch_training_observation_owner;
GRANT SELECT, UPDATE ON lifeswitch_training.workout_template
  TO lifeswitch_training_observation_owner;
GRANT SELECT ON lifeswitch_training.training_session, lifeswitch_training.training_set_log
  TO lifeswitch_training_observation_owner;

DO $grant$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brains_app') THEN
    EXECUTE 'GRANT SELECT ON lifeswitch_training.workout_template_role_event, lifeswitch_training.training_session_role_event TO brains_app';
    EXECUTE 'GRANT EXECUTE ON FUNCTION lifeswitch_training.set_workout_template_role(uuid, uuid, text, text, text) TO brains_app';
    EXECUTE 'GRANT EXECUTE ON FUNCTION lifeswitch_training.classify_unclassified_training_sessions(uuid, uuid, text, text, text) TO brains_app';
  END IF;
END
$grant$;

COMMENT ON COLUMN lifeswitch_training.workout_template.workout_role IS
  'User-controlled workout classification for future sessions: strength or rehab.';
COMMENT ON COLUMN lifeswitch_training.training_session.workout_role_snapshot IS
  'Immutable workout-template role captured when the session was completed.';

COMMIT;
