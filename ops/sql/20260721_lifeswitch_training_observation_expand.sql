BEGIN;

-- EXPAND PHASE
--
-- This migration preserves the current Training router while adding the
-- observation contract needed by its replacement. Existing UPDATE/DELETE
-- routes remain available during this phase, but every mutation is audited.
-- Enforcement is a later, one-way migration after the writer API is proven.

ALTER TABLE lifeswitch_training.training_session
  ADD COLUMN IF NOT EXISTS recorded_by_user_id uuid,
  ADD COLUMN IF NOT EXISTS idempotency_key text,
  ADD COLUMN IF NOT EXISTS source_snapshot jsonb,
  ADD COLUMN IF NOT EXISTS snapshot_schema_version smallint,
  ADD COLUMN IF NOT EXISTS snapshot_quality text,
  ADD COLUMN IF NOT EXISTS supersedes_training_session_id uuid,
  ADD COLUMN IF NOT EXISTS voided_at timestamptz,
  ADD COLUMN IF NOT EXISTS voided_by_user_id uuid,
  ADD COLUMN IF NOT EXISTS void_reason text;

ALTER TABLE lifeswitch_training.training_set_log
  ADD COLUMN IF NOT EXISTS capture_role text,
  ADD COLUMN IF NOT EXISTS load_unit text,
  ADD COLUMN IF NOT EXISTS source_snapshot jsonb,
  ADD COLUMN IF NOT EXISTS snapshot_schema_version smallint,
  ADD COLUMN IF NOT EXISTS snapshot_quality text;

ALTER TABLE lifeswitch_training.training_set_log_segment
  ADD COLUMN IF NOT EXISTS owner_user_id uuid,
  ADD COLUMN IF NOT EXISTS load_unit text,
  ADD COLUMN IF NOT EXISTS source_snapshot jsonb,
  ADD COLUMN IF NOT EXISTS snapshot_schema_version smallint,
  ADD COLUMN IF NOT EXISTS snapshot_quality text;

ALTER TABLE lifeswitch_training.conditioning_session_log
  ADD COLUMN IF NOT EXISTS recorded_by_user_id uuid,
  ADD COLUMN IF NOT EXISTS idempotency_key text,
  ADD COLUMN IF NOT EXISTS source_snapshot jsonb,
  ADD COLUMN IF NOT EXISTS snapshot_schema_version smallint,
  ADD COLUMN IF NOT EXISTS snapshot_quality text,
  ADD COLUMN IF NOT EXISTS supersedes_conditioning_session_id uuid,
  ADD COLUMN IF NOT EXISTS distance_value numeric,
  ADD COLUMN IF NOT EXISTS distance_unit text,
  ADD COLUMN IF NOT EXISTS voided_at timestamptz,
  ADD COLUMN IF NOT EXISTS voided_by_user_id uuid,
  ADD COLUMN IF NOT EXISTS void_reason text;

-- Preserve the original updated_at values while reconstructing provenance.
ALTER TABLE lifeswitch_training.training_session
  DISABLE TRIGGER trg_training_session_updated_at;
ALTER TABLE lifeswitch_training.training_set_log
  DISABLE TRIGGER trg_training_set_log_updated_at;
ALTER TABLE lifeswitch_training.training_set_log_segment
  DISABLE TRIGGER trg_training_set_log_segment_updated_at;
ALTER TABLE lifeswitch_training.conditioning_session_log
  DISABLE TRIGGER trg_conditioning_session_log_updated_at;

UPDATE lifeswitch_training.training_session s
SET
  recorded_by_user_id = coalesce(s.recorded_by_user_id, s.owner_user_id),
  source_snapshot = coalesce(
    s.source_snapshot,
    jsonb_build_object(
      'kind', 'training_session',
      'legacy_row', to_jsonb(s) - ARRAY[
        'recorded_by_user_id',
        'idempotency_key',
        'source_snapshot',
        'snapshot_schema_version',
        'snapshot_quality',
        'supersedes_training_session_id',
        'voided_at',
        'voided_by_user_id',
        'void_reason'
      ]
    )
  ),
  snapshot_schema_version = coalesce(s.snapshot_schema_version, 1),
  snapshot_quality = coalesce(s.snapshot_quality, 'legacy_reconstructed')
WHERE s.recorded_by_user_id IS NULL
   OR s.source_snapshot IS NULL
   OR s.snapshot_schema_version IS NULL
   OR s.snapshot_quality IS NULL;

-- NULL exercise_role_snapshot is historical ambiguity. It is never
-- reinterpreted from the current mutable exercise library.
UPDATE lifeswitch_training.training_set_log l
SET
  capture_role = coalesce(
    l.capture_role,
    CASE
      WHEN l.exercise_role_snapshot IN ('strength', 'rehab')
        THEN l.exercise_role_snapshot
      ELSE 'unknown'
    END
  ),
  load_unit = coalesce(l.load_unit, 'lb'),
  source_snapshot = coalesce(
    l.source_snapshot,
    jsonb_build_object(
      'kind', 'training_set',
      'legacy_row', to_jsonb(l) - ARRAY[
        'capture_role',
        'load_unit',
        'source_snapshot',
        'snapshot_schema_version',
        'snapshot_quality'
      ]
    )
  ),
  snapshot_schema_version = coalesce(l.snapshot_schema_version, 1),
  snapshot_quality = coalesce(
    l.snapshot_quality,
    CASE
      WHEN l.exercise_role_snapshot IN ('strength', 'rehab')
        THEN 'legacy_reconstructed'
      ELSE 'legacy_unknown'
    END
  )
WHERE l.capture_role IS NULL
   OR l.load_unit IS NULL
   OR l.source_snapshot IS NULL
   OR l.snapshot_schema_version IS NULL
   OR l.snapshot_quality IS NULL;

UPDATE lifeswitch_training.training_set_log_segment g
SET
  owner_user_id = coalesce(g.owner_user_id, l.owner_user_id),
  load_unit = coalesce(g.load_unit, l.load_unit, 'lb'),
  source_snapshot = coalesce(
    g.source_snapshot,
    jsonb_build_object(
      'kind', 'training_set_segment',
      'legacy_row', to_jsonb(g) - ARRAY[
        'owner_user_id',
        'load_unit',
        'source_snapshot',
        'snapshot_schema_version',
        'snapshot_quality'
      ]
    )
  ),
  snapshot_schema_version = coalesce(g.snapshot_schema_version, 1),
  snapshot_quality = coalesce(g.snapshot_quality, 'legacy_reconstructed')
FROM lifeswitch_training.training_set_log l
WHERE l.training_set_log_id = g.training_set_log_id
  AND (
    g.owner_user_id IS NULL
    OR g.load_unit IS NULL
    OR g.source_snapshot IS NULL
    OR g.snapshot_schema_version IS NULL
    OR g.snapshot_quality IS NULL
  );

UPDATE lifeswitch_training.conditioning_session_log c
SET
  recorded_by_user_id = coalesce(c.recorded_by_user_id, c.owner_user_id),
  source_snapshot = coalesce(
    c.source_snapshot,
    jsonb_build_object(
      'kind', 'conditioning_session',
      'legacy_row', to_jsonb(c) - ARRAY[
        'recorded_by_user_id',
        'idempotency_key',
        'source_snapshot',
        'snapshot_schema_version',
        'snapshot_quality',
        'supersedes_conditioning_session_id',
        'distance_value',
        'distance_unit',
        'voided_at',
        'voided_by_user_id',
        'void_reason'
      ],
      'prescription_reconstructed', to_jsonb(p)
    )
  ),
  snapshot_schema_version = coalesce(c.snapshot_schema_version, 1),
  snapshot_quality = coalesce(c.snapshot_quality, 'legacy_reconstructed')
FROM lifeswitch_training.my_conditioning_prescription p
WHERE p.my_conditioning_prescription_id IS NOT DISTINCT FROM
      c.my_conditioning_prescription_id
  AND (
    c.recorded_by_user_id IS NULL
    OR c.source_snapshot IS NULL
    OR c.snapshot_schema_version IS NULL
    OR c.snapshot_quality IS NULL
  );

-- The LEFT JOIN equivalent for conditioning rows without a prescription.
UPDATE lifeswitch_training.conditioning_session_log c
SET
  recorded_by_user_id = coalesce(c.recorded_by_user_id, c.owner_user_id),
  source_snapshot = coalesce(
    c.source_snapshot,
    jsonb_build_object(
      'kind', 'conditioning_session',
      'legacy_row', to_jsonb(c) - ARRAY[
        'recorded_by_user_id',
        'idempotency_key',
        'source_snapshot',
        'snapshot_schema_version',
        'snapshot_quality',
        'supersedes_conditioning_session_id',
        'distance_value',
        'distance_unit',
        'voided_at',
        'voided_by_user_id',
        'void_reason'
      ],
      'prescription_reconstructed', NULL
    )
  ),
  snapshot_schema_version = coalesce(c.snapshot_schema_version, 1),
  snapshot_quality = coalesce(c.snapshot_quality, 'legacy_reconstructed')
WHERE c.my_conditioning_prescription_id IS NULL
  AND (
    c.recorded_by_user_id IS NULL
    OR c.source_snapshot IS NULL
    OR c.snapshot_schema_version IS NULL
    OR c.snapshot_quality IS NULL
  );

ALTER TABLE lifeswitch_training.training_session
  ENABLE TRIGGER trg_training_session_updated_at;
ALTER TABLE lifeswitch_training.training_set_log
  ENABLE TRIGGER trg_training_set_log_updated_at;
ALTER TABLE lifeswitch_training.training_set_log_segment
  ENABLE TRIGGER trg_training_set_log_segment_updated_at;
ALTER TABLE lifeswitch_training.conditioning_session_log
  ENABLE TRIGGER trg_conditioning_session_log_updated_at;

-- Parent-side uniqueness supports owner-bound foreign keys.
CREATE UNIQUE INDEX IF NOT EXISTS ux_training_session_id_owner
  ON lifeswitch_training.training_session
  (training_session_id, owner_user_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_training_set_log_id_owner
  ON lifeswitch_training.training_set_log
  (training_set_log_id, owner_user_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_conditioning_prescription_id_owner
  ON lifeswitch_training.my_conditioning_prescription
  (my_conditioning_prescription_id, owner_user_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_conditioning_session_id_owner
  ON lifeswitch_training.conditioning_session_log
  (conditioning_session_log_id, owner_user_id);

DO $migration$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'training_set_log_session_owner_fkey'
      AND conrelid =
        'lifeswitch_training.training_set_log'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_set_log
      ADD CONSTRAINT training_set_log_session_owner_fkey
      FOREIGN KEY (training_session_id, owner_user_id)
      REFERENCES lifeswitch_training.training_session
        (training_session_id, owner_user_id)
      ON DELETE CASCADE
      NOT VALID;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'training_set_segment_set_owner_fkey'
      AND conrelid =
        'lifeswitch_training.training_set_log_segment'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_set_log_segment
      ADD CONSTRAINT training_set_segment_set_owner_fkey
      FOREIGN KEY (training_set_log_id, owner_user_id)
      REFERENCES lifeswitch_training.training_set_log
        (training_set_log_id, owner_user_id)
      ON DELETE CASCADE
      NOT VALID;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'training_session_supersedes_owner_fkey'
      AND conrelid =
        'lifeswitch_training.training_session'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_session
      ADD CONSTRAINT training_session_supersedes_owner_fkey
      FOREIGN KEY (supersedes_training_session_id, owner_user_id)
      REFERENCES lifeswitch_training.training_session
        (training_session_id, owner_user_id)
      ON DELETE RESTRICT
      NOT VALID;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'conditioning_session_prescription_owner_fkey'
      AND conrelid =
        'lifeswitch_training.conditioning_session_log'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.conditioning_session_log
      ADD CONSTRAINT conditioning_session_prescription_owner_fkey
      FOREIGN KEY (my_conditioning_prescription_id, owner_user_id)
      REFERENCES lifeswitch_training.my_conditioning_prescription
        (my_conditioning_prescription_id, owner_user_id)
      ON DELETE RESTRICT
      NOT VALID;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'conditioning_session_supersedes_owner_fkey'
      AND conrelid =
        'lifeswitch_training.conditioning_session_log'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.conditioning_session_log
      ADD CONSTRAINT conditioning_session_supersedes_owner_fkey
      FOREIGN KEY (supersedes_conditioning_session_id, owner_user_id)
      REFERENCES lifeswitch_training.conditioning_session_log
        (conditioning_session_log_id, owner_user_id)
      ON DELETE RESTRICT
      NOT VALID;
  END IF;
END
$migration$;

ALTER TABLE lifeswitch_training.training_set_log
  VALIDATE CONSTRAINT training_set_log_session_owner_fkey;
ALTER TABLE lifeswitch_training.training_set_log_segment
  VALIDATE CONSTRAINT training_set_segment_set_owner_fkey;
ALTER TABLE lifeswitch_training.training_session
  VALIDATE CONSTRAINT training_session_supersedes_owner_fkey;
ALTER TABLE lifeswitch_training.conditioning_session_log
  VALIDATE CONSTRAINT conditioning_session_prescription_owner_fkey;
ALTER TABLE lifeswitch_training.conditioning_session_log
  VALIDATE CONSTRAINT conditioning_session_supersedes_owner_fkey;

CREATE UNIQUE INDEX IF NOT EXISTS ux_training_session_owner_idempotency
  ON lifeswitch_training.training_session
  (owner_user_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_training_session_single_replacement
  ON lifeswitch_training.training_session
  (supersedes_training_session_id)
  WHERE supersedes_training_session_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_conditioning_session_owner_idempotency
  ON lifeswitch_training.conditioning_session_log
  (owner_user_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_conditioning_session_single_replacement
  ON lifeswitch_training.conditioning_session_log
  (supersedes_conditioning_session_id)
  WHERE supersedes_conditioning_session_id IS NOT NULL;

DO $migration$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'training_set_capture_role_allowed'
      AND conrelid =
        'lifeswitch_training.training_set_log'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_set_log
      ADD CONSTRAINT training_set_capture_role_allowed
      CHECK (capture_role IN ('strength', 'rehab', 'unknown'))
      NOT VALID;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'training_set_load_unit_allowed'
      AND conrelid =
        'lifeswitch_training.training_set_log'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_set_log
      ADD CONSTRAINT training_set_load_unit_allowed
      CHECK (load_unit IN ('lb', 'kg'))
      NOT VALID;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'training_segment_load_unit_allowed'
      AND conrelid =
        'lifeswitch_training.training_set_log_segment'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.training_set_log_segment
      ADD CONSTRAINT training_segment_load_unit_allowed
      CHECK (load_unit IN ('lb', 'kg'))
      NOT VALID;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'conditioning_distance_value_nonnegative'
      AND conrelid =
        'lifeswitch_training.conditioning_session_log'::regclass
  ) THEN
    ALTER TABLE lifeswitch_training.conditioning_session_log
      ADD CONSTRAINT conditioning_distance_value_nonnegative
      CHECK (distance_value IS NULL OR distance_value >= 0)
      NOT VALID;
  END IF;
END
$migration$;

COMMENT ON COLUMN lifeswitch_training.training_set_log.capture_role IS
  'Immutable role interpretation for this captured set. unknown means the legacy capture-time role cannot be proven.';

COMMENT ON COLUMN lifeswitch_training.training_set_log.exercise_role_snapshot IS
  'Legacy compatibility field. New and migrated rows are mirrored into capture_role; NULL legacy roles remain unknown and must never inherit the current mutable exercise role.';

COMMENT ON COLUMN lifeswitch_training.training_set_log.load_unit IS
  'Unit for captured load. Legacy rows are labeled lb under the original UI contract; snapshot_quality preserves reconstruction status.';

COMMENT ON COLUMN lifeswitch_training.conditioning_session_log.distance IS
  'Legacy free-text distance retained during expansion. New writers use distance_value and distance_unit.';

-- Transition capture functions populate omitted fields for legacy writers.
CREATE OR REPLACE FUNCTION
  lifeswitch_training.tg_transition_capture_training_session()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, lifeswitch_training
AS $function$
BEGIN
  NEW.recorded_by_user_id := coalesce(
    NEW.recorded_by_user_id,
    NEW.owner_user_id
  );
  IF NEW.source_snapshot IS NULL
     OR NEW.snapshot_schema_version IS NULL
     OR NEW.snapshot_quality IS NULL THEN
    NEW.source_snapshot := jsonb_build_object(
      'kind', 'training_session',
      'captured_row', to_jsonb(NEW) - ARRAY[
        'source_snapshot',
        'snapshot_schema_version',
        'snapshot_quality'
      ]
    );
    NEW.snapshot_schema_version := 1;
    NEW.snapshot_quality := 'captured';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION
  lifeswitch_training.tg_transition_capture_training_set_log()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, lifeswitch_training
AS $function$
DECLARE
  v_owner_user_id uuid;
BEGIN
  SELECT s.owner_user_id
    INTO v_owner_user_id
  FROM lifeswitch_training.training_session s
  WHERE s.training_session_id = NEW.training_session_id;

  IF v_owner_user_id IS NULL THEN
    RAISE EXCEPTION 'training session does not exist';
  END IF;
  IF NEW.owner_user_id IS DISTINCT FROM v_owner_user_id THEN
    RAISE EXCEPTION 'training set owner must match session owner';
  END IF;

  NEW.capture_role := coalesce(
    NEW.capture_role,
    NEW.exercise_role_snapshot,
    'unknown'
  );
  NEW.load_unit := coalesce(NEW.load_unit, 'lb');
  IF NEW.source_snapshot IS NULL
     OR NEW.snapshot_schema_version IS NULL
     OR NEW.snapshot_quality IS NULL THEN
    NEW.source_snapshot := jsonb_build_object(
      'kind', 'training_set',
      'captured_row', to_jsonb(NEW) - ARRAY[
        'source_snapshot',
        'snapshot_schema_version',
        'snapshot_quality'
      ]
    );
    NEW.snapshot_schema_version := 1;
    NEW.snapshot_quality := CASE
      WHEN NEW.capture_role = 'unknown' THEN 'captured_partial'
      ELSE 'captured'
    END;
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION
  lifeswitch_training.tg_transition_capture_training_set_log_segment()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, lifeswitch_training
AS $function$
DECLARE
  v_owner_user_id uuid;
  v_load_unit text;
BEGIN
  SELECT l.owner_user_id, l.load_unit
    INTO v_owner_user_id, v_load_unit
  FROM lifeswitch_training.training_set_log l
  WHERE l.training_set_log_id = NEW.training_set_log_id;

  IF v_owner_user_id IS NULL THEN
    RAISE EXCEPTION 'training set does not exist';
  END IF;
  IF NEW.owner_user_id IS NULL THEN
    NEW.owner_user_id := v_owner_user_id;
  ELSIF NEW.owner_user_id IS DISTINCT FROM v_owner_user_id THEN
    RAISE EXCEPTION 'training segment owner must match set owner';
  END IF;

  NEW.load_unit := coalesce(NEW.load_unit, v_load_unit, 'lb');
  IF NEW.source_snapshot IS NULL
     OR NEW.snapshot_schema_version IS NULL
     OR NEW.snapshot_quality IS NULL THEN
    NEW.source_snapshot := jsonb_build_object(
      'kind', 'training_set_segment',
      'captured_row', to_jsonb(NEW) - ARRAY[
        'source_snapshot',
        'snapshot_schema_version',
        'snapshot_quality'
      ]
    );
    NEW.snapshot_schema_version := 1;
    NEW.snapshot_quality := 'captured';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION
  lifeswitch_training.tg_transition_capture_conditioning_session_log()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, lifeswitch_training
AS $function$
DECLARE
  v_prescription jsonb;
  v_prescription_owner uuid;
BEGIN
  IF NEW.my_conditioning_prescription_id IS NOT NULL THEN
    SELECT to_jsonb(p), p.owner_user_id
      INTO v_prescription, v_prescription_owner
    FROM lifeswitch_training.my_conditioning_prescription p
    WHERE p.my_conditioning_prescription_id =
          NEW.my_conditioning_prescription_id;

    IF v_prescription IS NULL THEN
      RAISE EXCEPTION 'conditioning prescription does not exist';
    END IF;
    IF v_prescription_owner IS DISTINCT FROM NEW.owner_user_id THEN
      RAISE EXCEPTION
        'conditioning prescription must belong to session owner';
    END IF;
  END IF;

  NEW.recorded_by_user_id := coalesce(
    NEW.recorded_by_user_id,
    NEW.owner_user_id
  );
  IF NEW.source_snapshot IS NULL
     OR NEW.snapshot_schema_version IS NULL
     OR NEW.snapshot_quality IS NULL THEN
    NEW.source_snapshot := jsonb_build_object(
      'kind', 'conditioning_session',
      'captured_row', to_jsonb(NEW) - ARRAY[
        'source_snapshot',
        'snapshot_schema_version',
        'snapshot_quality'
      ],
      'prescription', v_prescription
    );
    NEW.snapshot_schema_version := 1;
    NEW.snapshot_quality := 'captured';
  END IF;
  RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS trg_transition_capture_training_session
  ON lifeswitch_training.training_session;
CREATE TRIGGER trg_transition_capture_training_session
BEFORE INSERT OR UPDATE ON lifeswitch_training.training_session
FOR EACH ROW
EXECUTE FUNCTION lifeswitch_training.tg_transition_capture_training_session();

DROP TRIGGER IF EXISTS trg_transition_capture_training_set_log
  ON lifeswitch_training.training_set_log;
CREATE TRIGGER trg_transition_capture_training_set_log
BEFORE INSERT OR UPDATE ON lifeswitch_training.training_set_log
FOR EACH ROW
EXECUTE FUNCTION lifeswitch_training.tg_transition_capture_training_set_log();

DROP TRIGGER IF EXISTS trg_transition_capture_training_set_log_segment
  ON lifeswitch_training.training_set_log_segment;
CREATE TRIGGER trg_transition_capture_training_set_log_segment
BEFORE INSERT OR UPDATE ON lifeswitch_training.training_set_log_segment
FOR EACH ROW
EXECUTE FUNCTION
  lifeswitch_training.tg_transition_capture_training_set_log_segment();

DROP TRIGGER IF EXISTS trg_transition_capture_conditioning_session_log
  ON lifeswitch_training.conditioning_session_log;
CREATE TRIGGER trg_transition_capture_conditioning_session_log
BEFORE INSERT OR UPDATE ON lifeswitch_training.conditioning_session_log
FOR EACH ROW
EXECUTE FUNCTION
  lifeswitch_training.tg_transition_capture_conditioning_session_log();

-- Event rows intentionally have no FK to observation tables. They must
-- survive legacy hard deletes during the expansion window.
CREATE TABLE IF NOT EXISTS lifeswitch_training.training_observation_event (
  training_observation_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  observation_type text NOT NULL CHECK (
    observation_type IN (
      'training_session',
      'training_set',
      'training_set_segment',
      'conditioning_session'
    )
  ),
  observation_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  actor_user_id uuid,
  event_type text NOT NULL CHECK (
    event_type IN (
      'legacy_backfilled',
      'created',
      'corrected',
      'voided',
      'legacy_updated',
      'legacy_deleted'
    )
  ),
  related_observation_id uuid,
  reason text,
  source text NOT NULL CHECK (source IN ('migration', 'database')),
  observation_snapshot jsonb NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_training_observation_event_observation
  ON lifeswitch_training.training_observation_event
  (observation_type, observation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_training_observation_event_owner
  ON lifeswitch_training.training_observation_event
  (owner_user_id, created_at DESC);

INSERT INTO lifeswitch_training.training_observation_event (
  observation_type,
  observation_id,
  owner_user_id,
  actor_user_id,
  event_type,
  source,
  observation_snapshot,
  metadata
)
SELECT
  legacy.observation_type,
  legacy.observation_id,
  legacy.owner_user_id,
  legacy.actor_user_id,
  'legacy_backfilled',
  'migration',
  legacy.observation_snapshot,
  legacy.metadata
FROM (
  SELECT
    'training_session'::text AS observation_type,
    s.training_session_id AS observation_id,
    s.owner_user_id,
    s.recorded_by_user_id AS actor_user_id,
    to_jsonb(s) AS observation_snapshot,
    jsonb_build_object('snapshot_quality', s.snapshot_quality) AS metadata
  FROM lifeswitch_training.training_session s
  UNION ALL
  SELECT
    'training_set',
    l.training_set_log_id,
    l.owner_user_id,
    l.owner_user_id,
    to_jsonb(l),
    jsonb_build_object(
      'snapshot_quality', l.snapshot_quality,
      'capture_role', l.capture_role
    )
  FROM lifeswitch_training.training_set_log l
  UNION ALL
  SELECT
    'training_set_segment',
    g.training_set_log_segment_id,
    g.owner_user_id,
    g.owner_user_id,
    to_jsonb(g),
    jsonb_build_object('snapshot_quality', g.snapshot_quality)
  FROM lifeswitch_training.training_set_log_segment g
  UNION ALL
  SELECT
    'conditioning_session',
    c.conditioning_session_log_id,
    c.owner_user_id,
    c.recorded_by_user_id,
    to_jsonb(c),
    jsonb_build_object('snapshot_quality', c.snapshot_quality)
  FROM lifeswitch_training.conditioning_session_log c
) legacy
WHERE NOT EXISTS (
  SELECT 1
  FROM lifeswitch_training.training_observation_event event
  WHERE event.observation_type = legacy.observation_type
    AND event.observation_id = legacy.observation_id
    AND event.event_type = 'legacy_backfilled'
);

CREATE OR REPLACE FUNCTION
  lifeswitch_training.tg_reject_training_observation_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, lifeswitch_training
AS $function$
BEGIN
  RAISE EXCEPTION 'training observation events are append-only';
END
$function$;

DROP TRIGGER IF EXISTS trg_training_observation_event_append_only
  ON lifeswitch_training.training_observation_event;
CREATE TRIGGER trg_training_observation_event_append_only
BEFORE UPDATE OR DELETE
ON lifeswitch_training.training_observation_event
FOR EACH ROW
EXECUTE FUNCTION
  lifeswitch_training.tg_reject_training_observation_event_mutation();

DROP TRIGGER IF EXISTS trg_training_observation_event_truncate
  ON lifeswitch_training.training_observation_event;
CREATE TRIGGER trg_training_observation_event_truncate
BEFORE TRUNCATE ON lifeswitch_training.training_observation_event
FOR EACH STATEMENT
EXECUTE FUNCTION
  lifeswitch_training.tg_reject_training_observation_event_mutation();

CREATE OR REPLACE FUNCTION
  lifeswitch_training.tg_reject_training_observation_truncate()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, lifeswitch_training
AS $function$
BEGIN
  RAISE EXCEPTION
    'training observations cannot be truncated; void observations instead';
END
$function$;

DROP TRIGGER IF EXISTS trg_training_session_reject_truncate
  ON lifeswitch_training.training_session;
CREATE TRIGGER trg_training_session_reject_truncate
BEFORE TRUNCATE ON lifeswitch_training.training_session
FOR EACH STATEMENT
EXECUTE FUNCTION
  lifeswitch_training.tg_reject_training_observation_truncate();

DROP TRIGGER IF EXISTS trg_training_set_log_reject_truncate
  ON lifeswitch_training.training_set_log;
CREATE TRIGGER trg_training_set_log_reject_truncate
BEFORE TRUNCATE ON lifeswitch_training.training_set_log
FOR EACH STATEMENT
EXECUTE FUNCTION
  lifeswitch_training.tg_reject_training_observation_truncate();

DROP TRIGGER IF EXISTS trg_training_set_log_segment_reject_truncate
  ON lifeswitch_training.training_set_log_segment;
CREATE TRIGGER trg_training_set_log_segment_reject_truncate
BEFORE TRUNCATE ON lifeswitch_training.training_set_log_segment
FOR EACH STATEMENT
EXECUTE FUNCTION
  lifeswitch_training.tg_reject_training_observation_truncate();

DROP TRIGGER IF EXISTS trg_conditioning_session_log_reject_truncate
  ON lifeswitch_training.conditioning_session_log;
CREATE TRIGGER trg_conditioning_session_log_reject_truncate
BEFORE TRUNCATE ON lifeswitch_training.conditioning_session_log
FOR EACH STATEMENT
EXECUTE FUNCTION
  lifeswitch_training.tg_reject_training_observation_truncate();

-- One audit function is parameterized by observation type and identifier.
CREATE OR REPLACE FUNCTION
  lifeswitch_training.tg_transition_audit_training_observation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, lifeswitch_training
AS $function$
DECLARE
  v_new jsonb;
  v_old jsonb;
  v_observation_id uuid;
  v_owner_user_id uuid;
  v_actor_user_id uuid;
  v_related_observation_id uuid;
  v_event_type text;
  v_reason text;
BEGIN
  IF TG_OP <> 'DELETE' THEN
    v_new := to_jsonb(NEW);
  END IF;
  IF TG_OP <> 'INSERT' THEN
    v_old := to_jsonb(OLD);
  END IF;

  v_observation_id := coalesce(
    nullif(v_new ->> TG_ARGV[1], '')::uuid,
    nullif(v_old ->> TG_ARGV[1], '')::uuid
  );
  v_owner_user_id := coalesce(
    nullif(v_new ->> 'owner_user_id', '')::uuid,
    nullif(v_old ->> 'owner_user_id', '')::uuid
  );
  v_actor_user_id := coalesce(
    nullif(v_new ->> 'voided_by_user_id', '')::uuid,
    nullif(v_new ->> 'recorded_by_user_id', '')::uuid,
    v_owner_user_id
  );

  IF TG_OP = 'INSERT' THEN
    IF array_length(TG_ARGV, 1) >= 3 THEN
      v_related_observation_id :=
        nullif(v_new ->> TG_ARGV[2], '')::uuid;
    END IF;
    v_event_type := CASE
      WHEN v_related_observation_id IS NULL THEN 'created'
      ELSE 'corrected'
    END;
  ELSIF TG_OP = 'UPDATE' THEN
    IF to_jsonb(OLD) IS NOT DISTINCT FROM to_jsonb(NEW) THEN
      RETURN NEW;
    END IF;
    IF v_old ->> 'voided_at' IS NULL
       AND v_new ->> 'voided_at' IS NOT NULL THEN
      v_event_type := 'voided';
      v_reason := v_new ->> 'void_reason';
    ELSE
      v_event_type := 'legacy_updated';
    END IF;
  ELSE
    v_event_type := 'legacy_deleted';
  END IF;

  INSERT INTO lifeswitch_training.training_observation_event (
    observation_type,
    observation_id,
    owner_user_id,
    actor_user_id,
    event_type,
    related_observation_id,
    reason,
    source,
    observation_snapshot
  ) VALUES (
    TG_ARGV[0],
    v_observation_id,
    v_owner_user_id,
    v_actor_user_id,
    v_event_type,
    v_related_observation_id,
    v_reason,
    'database',
    CASE
      WHEN TG_OP = 'INSERT' THEN v_new
      WHEN TG_OP = 'UPDATE' THEN
        jsonb_build_object('before', to_jsonb(OLD), 'after', to_jsonb(NEW))
      ELSE to_jsonb(OLD)
    END
  );

  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS trg_transition_audit_training_session
  ON lifeswitch_training.training_session;
CREATE TRIGGER trg_transition_audit_training_session
AFTER INSERT OR UPDATE OR DELETE ON lifeswitch_training.training_session
FOR EACH ROW
EXECUTE FUNCTION lifeswitch_training.tg_transition_audit_training_observation(
  'training_session',
  'training_session_id',
  'supersedes_training_session_id'
);

DROP TRIGGER IF EXISTS trg_transition_audit_training_set_log
  ON lifeswitch_training.training_set_log;
CREATE TRIGGER trg_transition_audit_training_set_log
AFTER INSERT OR UPDATE OR DELETE ON lifeswitch_training.training_set_log
FOR EACH ROW
EXECUTE FUNCTION lifeswitch_training.tg_transition_audit_training_observation(
  'training_set',
  'training_set_log_id'
);

DROP TRIGGER IF EXISTS trg_transition_audit_training_set_log_segment
  ON lifeswitch_training.training_set_log_segment;
CREATE TRIGGER trg_transition_audit_training_set_log_segment
AFTER INSERT OR UPDATE OR DELETE
ON lifeswitch_training.training_set_log_segment
FOR EACH ROW
EXECUTE FUNCTION lifeswitch_training.tg_transition_audit_training_observation(
  'training_set_segment',
  'training_set_log_segment_id'
);

DROP TRIGGER IF EXISTS trg_transition_audit_conditioning_session_log
  ON lifeswitch_training.conditioning_session_log;
CREATE TRIGGER trg_transition_audit_conditioning_session_log
AFTER INSERT OR UPDATE OR DELETE
ON lifeswitch_training.conditioning_session_log
FOR EACH ROW
EXECUTE FUNCTION lifeswitch_training.tg_transition_audit_training_observation(
  'conditioning_session',
  'conditioning_session_log_id',
  'supersedes_conditioning_session_id'
);

CREATE OR REPLACE VIEW lifeswitch_training.training_session_current_v AS
SELECT s.*
FROM lifeswitch_training.training_session s
WHERE s.voided_at IS NULL
  AND s.is_active = true
  AND NOT EXISTS (
    SELECT 1
    FROM lifeswitch_training.training_session replacement
    WHERE replacement.supersedes_training_session_id = s.training_session_id
  );

CREATE OR REPLACE VIEW lifeswitch_training.conditioning_session_current_v AS
SELECT c.*
FROM lifeswitch_training.conditioning_session_log c
WHERE c.voided_at IS NULL
  AND c.is_active = true
  AND NOT EXISTS (
    SELECT 1
    FROM lifeswitch_training.conditioning_session_log replacement
    WHERE replacement.supersedes_conditioning_session_id = c.conditioning_session_log_id
  );

REVOKE ALL ON FUNCTION
  lifeswitch_training.tg_transition_capture_training_session()
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  lifeswitch_training.tg_transition_capture_training_set_log()
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  lifeswitch_training.tg_transition_capture_training_set_log_segment()
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  lifeswitch_training.tg_transition_capture_conditioning_session_log()
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  lifeswitch_training.tg_reject_training_observation_event_mutation()
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  lifeswitch_training.tg_reject_training_observation_truncate()
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  lifeswitch_training.tg_transition_audit_training_observation()
FROM PUBLIC;

REVOKE ALL ON TABLE
  lifeswitch_training.training_observation_event
FROM PUBLIC;

DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brains_app') THEN
    GRANT SELECT, INSERT ON TABLE
      lifeswitch_training.training_observation_event
    TO brains_app;
    GRANT SELECT ON TABLE
      lifeswitch_training.training_session_current_v,
      lifeswitch_training.conditioning_session_current_v
    TO brains_app;
  END IF;
END
$grants$;

COMMIT;
