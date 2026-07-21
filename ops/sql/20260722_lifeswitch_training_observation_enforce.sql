BEGIN;

-- ENFORCE PHASE
--
-- Run once as a PostgreSQL superuser after the 20260721 expansion. This
-- transfers Training observation objects to a protected NOLOGIN role,
-- validates the expanded observation contract, makes captured rows immutable,
-- and removes direct application writes. The 20260723 writer API is installed
-- immediately afterward, before application traffic is cut over.

DO $preflight$
DECLARE
  v_owner constant text := 'lifeswitch_training_observation_owner';
  v_role pg_roles%ROWTYPE;
  v_missing text;
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_roles AS r
    WHERE r.rolname = current_user
      AND r.rolsuper
  ) THEN
    RAISE EXCEPTION
      'training observation enforcement must be run by a PostgreSQL superuser';
  END IF;

  SELECT string_agg(required_object, ', ' ORDER BY required_object)
    INTO v_missing
  FROM (
    VALUES
      ('lifeswitch_training.training_session'),
      ('lifeswitch_training.training_set_log'),
      ('lifeswitch_training.training_set_log_segment'),
      ('lifeswitch_training.conditioning_session_log'),
      ('lifeswitch_training.training_observation_event')
  ) AS required(required_object)
  WHERE to_regclass(required_object) IS NULL;

  IF v_missing IS NOT NULL THEN
    RAISE EXCEPTION
      'training observation enforcement is missing required relations: %',
      v_missing;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_owner) THEN
    EXECUTE format(
      'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
      v_owner
    );
  ELSE
    SELECT * INTO v_role FROM pg_roles WHERE rolname = v_owner;
    IF v_role.rolcanlogin
       OR v_role.rolsuper
       OR v_role.rolcreatedb
       OR v_role.rolcreaterole
       OR v_role.rolinherit
       OR v_role.rolreplication
       OR v_role.rolbypassrls THEN
      RAISE EXCEPTION
        'protected training observation owner role has unsafe attributes';
    END IF;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brains_app')
     AND pg_has_role('brains_app', v_owner, 'member') THEN
    RAISE EXCEPTION
      'brains_app must not be a member of the protected training observation owner role';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_trigger AS t
    WHERE t.tgrelid = 'lifeswitch_training.training_session'::regclass
      AND t.tgname = 'trg_enforce_training_session_immutable'
      AND NOT t.tgisinternal
  ) OR (
    SELECT pg_get_userbyid(c.relowner) = v_owner
    FROM pg_class AS c
    WHERE c.oid = 'lifeswitch_training.training_session'::regclass
  ) THEN
    RAISE EXCEPTION
      'training observation enforcement already applied; this migration is one-shot';
  END IF;
END;
$preflight$;

LOCK TABLE lifeswitch_training.training_session IN ACCESS EXCLUSIVE MODE;
LOCK TABLE lifeswitch_training.training_set_log IN ACCESS EXCLUSIVE MODE;
LOCK TABLE lifeswitch_training.training_set_log_segment IN ACCESS EXCLUSIVE MODE;
LOCK TABLE lifeswitch_training.conditioning_session_log IN ACCESS EXCLUSIVE MODE;
LOCK TABLE lifeswitch_training.training_observation_event IN ACCESS EXCLUSIVE MODE;

DO $validate_contract$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM lifeswitch_training.training_session AS s
    WHERE s.owner_user_id IS NULL
       OR s.recorded_by_user_id IS NULL
       OR s.source_snapshot IS NULL
       OR s.source_snapshot = '{}'::jsonb
       OR s.snapshot_schema_version IS NULL
       OR s.snapshot_quality IS NULL
  ) THEN
    RAISE EXCEPTION
      'training session enforcement blocked: incomplete provenance contract';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM lifeswitch_training.training_set_log AS l
    WHERE l.owner_user_id IS NULL
       OR l.capture_role IS NULL
       OR l.load_unit IS NULL
       OR l.source_snapshot IS NULL
       OR l.source_snapshot = '{}'::jsonb
       OR l.snapshot_schema_version IS NULL
       OR l.snapshot_quality IS NULL
       OR l.weight < 0
       OR l.reps < 0
       OR l.volume < 0
  ) THEN
    RAISE EXCEPTION
      'training set enforcement blocked: incomplete or invalid capture contract';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM lifeswitch_training.training_set_log_segment AS g
    WHERE g.owner_user_id IS NULL
       OR g.load_unit IS NULL
       OR g.source_snapshot IS NULL
       OR g.source_snapshot = '{}'::jsonb
       OR g.snapshot_schema_version IS NULL
       OR g.snapshot_quality IS NULL
       OR g.weight < 0
       OR g.reps < 0
       OR g.volume < 0
  ) THEN
    RAISE EXCEPTION
      'training segment enforcement blocked: incomplete or invalid capture contract';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM lifeswitch_training.conditioning_session_log AS c
    WHERE c.owner_user_id IS NULL
       OR c.recorded_by_user_id IS NULL
       OR c.source_snapshot IS NULL
       OR c.source_snapshot = '{}'::jsonb
       OR c.snapshot_schema_version IS NULL
       OR c.snapshot_quality IS NULL
       OR c.duration_min < 0
       OR c.distance_value < 0
  ) THEN
    RAISE EXCEPTION
      'conditioning session enforcement blocked: incomplete or invalid capture contract';
  END IF;
END;
$validate_contract$;

ALTER TABLE lifeswitch_training.training_session
  ALTER COLUMN recorded_by_user_id SET NOT NULL,
  ALTER COLUMN source_snapshot SET NOT NULL,
  ALTER COLUMN snapshot_schema_version SET NOT NULL,
  ALTER COLUMN snapshot_quality SET NOT NULL;

ALTER TABLE lifeswitch_training.training_set_log
  ALTER COLUMN capture_role SET NOT NULL,
  ALTER COLUMN load_unit SET NOT NULL,
  ALTER COLUMN source_snapshot SET NOT NULL,
  ALTER COLUMN snapshot_schema_version SET NOT NULL,
  ALTER COLUMN snapshot_quality SET NOT NULL;

ALTER TABLE lifeswitch_training.training_set_log_segment
  ALTER COLUMN owner_user_id SET NOT NULL,
  ALTER COLUMN load_unit SET NOT NULL,
  ALTER COLUMN source_snapshot SET NOT NULL,
  ALTER COLUMN snapshot_schema_version SET NOT NULL,
  ALTER COLUMN snapshot_quality SET NOT NULL;

ALTER TABLE lifeswitch_training.conditioning_session_log
  ALTER COLUMN recorded_by_user_id SET NOT NULL,
  ALTER COLUMN source_snapshot SET NOT NULL,
  ALTER COLUMN snapshot_schema_version SET NOT NULL,
  ALTER COLUMN snapshot_quality SET NOT NULL;

DO $constraints$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'training_session_void_contract'
      AND conrelid = 'lifeswitch_training.training_session'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_session
      ADD CONSTRAINT training_session_void_contract CHECK (
        (voided_at IS NULL AND voided_by_user_id IS NULL AND void_reason IS NULL)
        OR
        (voided_at IS NOT NULL AND voided_by_user_id IS NOT NULL
          AND nullif(btrim(void_reason), '') IS NOT NULL AND is_active = false)
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'training_session_time_order'
      AND conrelid = 'lifeswitch_training.training_session'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_session
      ADD CONSTRAINT training_session_time_order CHECK (
        started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'training_set_nonnegative_values'
      AND conrelid = 'lifeswitch_training.training_set_log'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_set_log
      ADD CONSTRAINT training_set_nonnegative_values CHECK (
        exercise_sort_order >= 0 AND set_index >= 1
        AND weight >= 0 AND reps >= 0 AND volume >= 0
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'training_segment_nonnegative_values'
      AND conrelid = 'lifeswitch_training.training_set_log_segment'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_set_log_segment
      ADD CONSTRAINT training_segment_nonnegative_values CHECK (
        segment_index >= 1 AND weight >= 0 AND reps >= 0 AND volume >= 0
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'conditioning_session_void_contract'
      AND conrelid = 'lifeswitch_training.conditioning_session_log'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.conditioning_session_log
      ADD CONSTRAINT conditioning_session_void_contract CHECK (
        (voided_at IS NULL AND voided_by_user_id IS NULL AND void_reason IS NULL)
        OR
        (voided_at IS NOT NULL AND voided_by_user_id IS NOT NULL
          AND nullif(btrim(void_reason), '') IS NOT NULL AND is_active = false)
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'conditioning_session_value_contract'
      AND conrelid = 'lifeswitch_training.conditioning_session_log'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.conditioning_session_log
      ADD CONSTRAINT conditioning_session_value_contract CHECK (
        duration_min >= 0
        AND (heart_rate_avg IS NULL OR heart_rate_avg >= 0)
        AND (distance_value IS NULL OR distance_value >= 0)
        AND (
          (distance_value IS NULL AND distance_unit IS NULL)
          OR
          (distance_value IS NOT NULL
            AND distance_unit IN ('mi', 'km', 'm', 'yd', 'ft'))
        )
      );
  END IF;
END;
$constraints$;

ALTER TABLE lifeswitch_training.training_set_log
  VALIDATE CONSTRAINT training_set_capture_role_allowed;
ALTER TABLE lifeswitch_training.training_set_log
  VALIDATE CONSTRAINT training_set_load_unit_allowed;
ALTER TABLE lifeswitch_training.training_set_log_segment
  VALIDATE CONSTRAINT training_segment_load_unit_allowed;
ALTER TABLE lifeswitch_training.conditioning_session_log
  VALIDATE CONSTRAINT conditioning_distance_value_nonnegative;

CREATE OR REPLACE FUNCTION
  lifeswitch_training.tg_enforce_training_parent_immutable()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION
      'training observations cannot be deleted; void the parent observation';
  END IF;

  IF OLD.voided_at IS NULL
     AND NEW.voided_at IS NOT NULL
     AND NEW.voided_by_user_id IS NOT NULL
     AND nullif(btrim(NEW.void_reason), '') IS NOT NULL
     AND NEW.is_active = false
     AND (to_jsonb(NEW) - ARRAY[
       'voided_at', 'voided_by_user_id', 'void_reason',
       'is_active', 'updated_at'
     ]) IS NOT DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
         'voided_at', 'voided_by_user_id', 'void_reason',
         'is_active', 'updated_at'
       ]) THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION
    'captured training observations are immutable; correct with a replacement observation';
END;
$function$;

CREATE OR REPLACE FUNCTION
  lifeswitch_training.tg_enforce_training_child_immutable()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION
    'captured training observation children are immutable; correct the complete session';
END;
$function$;

DROP TRIGGER IF EXISTS trg_enforce_training_session_immutable
  ON lifeswitch_training.training_session;
CREATE TRIGGER trg_enforce_training_session_immutable
BEFORE UPDATE OR DELETE ON lifeswitch_training.training_session
FOR EACH ROW
EXECUTE FUNCTION lifeswitch_training.tg_enforce_training_parent_immutable();

DROP TRIGGER IF EXISTS trg_enforce_training_set_log_immutable
  ON lifeswitch_training.training_set_log;
CREATE TRIGGER trg_enforce_training_set_log_immutable
BEFORE UPDATE OR DELETE ON lifeswitch_training.training_set_log
FOR EACH ROW
EXECUTE FUNCTION lifeswitch_training.tg_enforce_training_child_immutable();

DROP TRIGGER IF EXISTS trg_enforce_training_set_segment_immutable
  ON lifeswitch_training.training_set_log_segment;
CREATE TRIGGER trg_enforce_training_set_segment_immutable
BEFORE UPDATE OR DELETE ON lifeswitch_training.training_set_log_segment
FOR EACH ROW
EXECUTE FUNCTION lifeswitch_training.tg_enforce_training_child_immutable();

DROP TRIGGER IF EXISTS trg_enforce_conditioning_session_immutable
  ON lifeswitch_training.conditioning_session_log;
CREATE TRIGGER trg_enforce_conditioning_session_immutable
BEFORE UPDATE OR DELETE ON lifeswitch_training.conditioning_session_log
FOR EACH ROW
EXECUTE FUNCTION lifeswitch_training.tg_enforce_training_parent_immutable();

REVOKE ALL ON TABLE lifeswitch_training.training_session FROM PUBLIC;
REVOKE ALL ON TABLE lifeswitch_training.training_set_log FROM PUBLIC;
REVOKE ALL ON TABLE lifeswitch_training.training_set_log_segment FROM PUBLIC;
REVOKE ALL ON TABLE lifeswitch_training.conditioning_session_log FROM PUBLIC;
REVOKE ALL ON TABLE lifeswitch_training.training_observation_event FROM PUBLIC;
REVOKE ALL ON TABLE lifeswitch_training.training_session_current_v FROM PUBLIC;
REVOKE ALL ON TABLE lifeswitch_training.conditioning_session_current_v FROM PUBLIC;

DO $acl$
DECLARE
  v_role_name text;
  v_column record;
BEGIN
  FOREACH v_role_name IN ARRAY ARRAY['anon', 'authenticated', 'brains_app']
  LOOP
    CONTINUE WHEN NOT EXISTS (
      SELECT 1 FROM pg_roles WHERE rolname = v_role_name
    );

    EXECUTE format(
      'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE %s FROM %I',
      'lifeswitch_training.training_session, '
      'lifeswitch_training.training_set_log, '
      'lifeswitch_training.training_set_log_segment, '
      'lifeswitch_training.conditioning_session_log, '
      'lifeswitch_training.training_observation_event',
      v_role_name
    );

    -- Table-level REVOKE does not remove an older column-level grant.
    FOR v_column IN
      SELECT DISTINCT
        p.table_schema,
        p.table_name,
        p.column_name
      FROM information_schema.column_privileges AS p
      WHERE p.grantee = v_role_name
        AND p.privilege_type IN ('INSERT', 'UPDATE')
        AND (p.table_schema, p.table_name) IN (
          ('lifeswitch_training', 'training_session'),
          ('lifeswitch_training', 'training_set_log'),
          ('lifeswitch_training', 'training_set_log_segment'),
          ('lifeswitch_training', 'conditioning_session_log'),
          ('lifeswitch_training', 'training_observation_event')
        )
    LOOP
      EXECUTE format(
        'REVOKE INSERT (%I), UPDATE (%I) ON TABLE %I.%I FROM %I',
        v_column.column_name,
        v_column.column_name,
        v_column.table_schema,
        v_column.table_name,
        v_role_name
      );
    END LOOP;
  END LOOP;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brains_app') THEN
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
  END IF;
END;
$acl$;

GRANT USAGE ON SCHEMA lifeswitch_training
  TO lifeswitch_training_observation_owner;
GRANT SELECT ON TABLE
  lifeswitch_training.my_exercise,
  lifeswitch_training.workout_template,
  lifeswitch_training.workout_template_exercise,
  lifeswitch_training.workout_template_exercise_segment,
  lifeswitch_training.my_conditioning_prescription,
  lifeswitch_training.conditioning_library
TO lifeswitch_training_observation_owner;

ALTER TABLE lifeswitch_training.training_session
  OWNER TO lifeswitch_training_observation_owner;
ALTER TABLE lifeswitch_training.training_set_log
  OWNER TO lifeswitch_training_observation_owner;
ALTER TABLE lifeswitch_training.training_set_log_segment
  OWNER TO lifeswitch_training_observation_owner;
ALTER TABLE lifeswitch_training.conditioning_session_log
  OWNER TO lifeswitch_training_observation_owner;
ALTER TABLE lifeswitch_training.training_observation_event
  OWNER TO lifeswitch_training_observation_owner;
ALTER VIEW lifeswitch_training.training_session_current_v
  OWNER TO lifeswitch_training_observation_owner;
ALTER VIEW lifeswitch_training.conditioning_session_current_v
  OWNER TO lifeswitch_training_observation_owner;

ALTER FUNCTION lifeswitch_training.tg_transition_capture_training_session()
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training.tg_transition_capture_training_set_log()
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION
  lifeswitch_training.tg_transition_capture_training_set_log_segment()
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION
  lifeswitch_training.tg_transition_capture_conditioning_session_log()
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION
  lifeswitch_training.tg_transition_audit_training_observation()
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION
  lifeswitch_training.tg_reject_training_observation_event_mutation()
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training.tg_reject_training_observation_truncate()
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training.tg_enforce_training_parent_immutable()
  OWNER TO lifeswitch_training_observation_owner;
ALTER FUNCTION lifeswitch_training.tg_enforce_training_child_immutable()
  OWNER TO lifeswitch_training_observation_owner;

REVOKE ALL ON FUNCTION
  lifeswitch_training.tg_enforce_training_parent_immutable()
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  lifeswitch_training.tg_enforce_training_child_immutable()
FROM PUBLIC;

DO $function_acl$
DECLARE
  v_role_name text;
  v_function regprocedure;
BEGIN
  FOREACH v_role_name IN ARRAY ARRAY['anon', 'authenticated', 'brains_app']
  LOOP
    CONTINUE WHEN NOT EXISTS (
      SELECT 1 FROM pg_roles WHERE rolname = v_role_name
    );

    FOREACH v_function IN ARRAY ARRAY[
      'lifeswitch_training.tg_transition_capture_training_session()'::regprocedure,
      'lifeswitch_training.tg_transition_capture_training_set_log()'::regprocedure,
      'lifeswitch_training.tg_transition_capture_training_set_log_segment()'::regprocedure,
      'lifeswitch_training.tg_transition_capture_conditioning_session_log()'::regprocedure,
      'lifeswitch_training.tg_transition_audit_training_observation()'::regprocedure,
      'lifeswitch_training.tg_reject_training_observation_event_mutation()'::regprocedure,
      'lifeswitch_training.tg_reject_training_observation_truncate()'::regprocedure,
      'lifeswitch_training.tg_enforce_training_parent_immutable()'::regprocedure,
      'lifeswitch_training.tg_enforce_training_child_immutable()'::regprocedure
    ]
    LOOP
      EXECUTE format(
        'REVOKE ALL ON FUNCTION %s FROM %I',
        v_function,
        v_role_name
      );
    END LOOP;
  END LOOP;
END;
$function_acl$;

DO $acl_postflight$
DECLARE
  v_role_name text;
  v_relation regclass;
  v_function regprocedure;
BEGIN
  FOREACH v_role_name IN ARRAY ARRAY['anon', 'authenticated', 'brains_app']
  LOOP
    CONTINUE WHEN NOT EXISTS (
      SELECT 1 FROM pg_roles WHERE rolname = v_role_name
    );

    FOREACH v_relation IN ARRAY ARRAY[
      'lifeswitch_training.training_session'::regclass,
      'lifeswitch_training.training_set_log'::regclass,
      'lifeswitch_training.training_set_log_segment'::regclass,
      'lifeswitch_training.conditioning_session_log'::regclass,
      'lifeswitch_training.training_observation_event'::regclass
    ]
    LOOP
      IF has_table_privilege(
           v_role_name,
           v_relation,
           'INSERT, UPDATE, DELETE, TRUNCATE'
         )
         OR has_any_column_privilege(
           v_role_name,
           v_relation,
           'INSERT, UPDATE'
         ) THEN
        RAISE EXCEPTION
          'raw Training observation write privilege remains for role % on %',
          v_role_name,
          v_relation;
      END IF;
    END LOOP;

    FOREACH v_function IN ARRAY ARRAY[
      'lifeswitch_training.tg_transition_capture_training_session()'::regprocedure,
      'lifeswitch_training.tg_transition_capture_training_set_log()'::regprocedure,
      'lifeswitch_training.tg_transition_capture_training_set_log_segment()'::regprocedure,
      'lifeswitch_training.tg_transition_capture_conditioning_session_log()'::regprocedure,
      'lifeswitch_training.tg_transition_audit_training_observation()'::regprocedure,
      'lifeswitch_training.tg_reject_training_observation_event_mutation()'::regprocedure,
      'lifeswitch_training.tg_reject_training_observation_truncate()'::regprocedure,
      'lifeswitch_training.tg_enforce_training_parent_immutable()'::regprocedure,
      'lifeswitch_training.tg_enforce_training_child_immutable()'::regprocedure
    ]
    LOOP
      IF has_function_privilege(v_role_name, v_function, 'EXECUTE') THEN
        RAISE EXCEPTION
          'protected Training helper execution remains for role % on %',
          v_role_name,
          v_function;
      END IF;
    END LOOP;
  END LOOP;

  IF EXISTS (
    SELECT 1
    FROM pg_class AS c
    CROSS JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) AS a
    WHERE c.oid IN (
      'lifeswitch_training.training_session'::regclass,
      'lifeswitch_training.training_set_log'::regclass,
      'lifeswitch_training.training_set_log_segment'::regclass,
      'lifeswitch_training.conditioning_session_log'::regclass,
      'lifeswitch_training.training_observation_event'::regclass
    )
      AND a.grantee = 0
      AND a.privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
  ) THEN
    RAISE EXCEPTION 'PUBLIC retains a raw Training observation write privilege';
  END IF;
END;
$acl_postflight$;

COMMIT;
