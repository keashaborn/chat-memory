BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 artifact migration must run as sage, current_user=%', current_user;
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS memory.artifact (
  artifact_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  artifact_kind text NOT NULL,
  title text,
  media_type text NOT NULL DEFAULT 'text/plain',
  authorship text NOT NULL DEFAULT 'unknown',
  content text NOT NULL,
  content_sha256 text NOT NULL,
  extraction_policy text NOT NULL DEFAULT 'review_only',
  sensitivity memory.sensitivity_level NOT NULL DEFAULT 'medium',
  status memory.record_status NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, content_sha256),
  UNIQUE (owner_user_id, artifact_id),
  CHECK (btrim(artifact_kind) <> ''),
  CHECK (btrim(media_type) <> ''),
  CHECK (authorship IN ('user', 'assistant', 'external', 'mixed', 'unknown')),
  CHECK (btrim(content) <> ''),
  CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (extraction_policy IN ('blocked', 'review_only', 'eligible'))
);

CREATE INDEX IF NOT EXISTS artifact_owner_status_time_idx
  ON memory.artifact(owner_user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS memory.artifact_occurrence (
  occurrence_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  artifact_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  occurrence_key text NOT NULL,
  relation text NOT NULL,
  observed_authorship text NOT NULL DEFAULT 'unknown',
  source_char_start integer,
  source_char_end integer,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, occurrence_key),
  UNIQUE (owner_user_id, occurrence_id),
  FOREIGN KEY (owner_user_id, artifact_id)
    REFERENCES memory.artifact(owner_user_id, artifact_id)
    ON DELETE CASCADE,
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  CHECK (btrim(occurrence_key) <> ''),
  CHECK (relation IN ('submitted', 'quoted', 'attached', 'generated', 'imported')),
  CHECK (observed_authorship IN ('user', 'assistant', 'external', 'mixed', 'unknown')),
  CHECK (
    (source_char_start IS NULL AND source_char_end IS NULL)
    OR (
      source_char_start IS NOT NULL
      AND source_char_end IS NOT NULL
      AND source_char_start >= 0
      AND source_char_end > source_char_start
    )
  )
);

CREATE INDEX IF NOT EXISTS artifact_occurrence_owner_artifact_idx
  ON memory.artifact_occurrence(owner_user_id, artifact_id, created_at DESC);

CREATE INDEX IF NOT EXISTS artifact_occurrence_owner_evidence_idx
  ON memory.artifact_occurrence(owner_user_id, evidence_id);

CREATE TABLE IF NOT EXISTS memory.artifact_section (
  section_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  artifact_id uuid NOT NULL,
  parent_section_id uuid,
  ordinal integer NOT NULL,
  level integer NOT NULL DEFAULT 1,
  heading text,
  content text NOT NULL,
  content_sha256 text NOT NULL,
  char_start integer NOT NULL,
  char_end integer NOT NULL,
  authorship text NOT NULL DEFAULT 'unknown',
  retrieval_eligible boolean NOT NULL DEFAULT false,
  promotion_eligible boolean NOT NULL DEFAULT false,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, artifact_id, ordinal),
  UNIQUE (owner_user_id, artifact_id, section_id),
  FOREIGN KEY (owner_user_id, artifact_id)
    REFERENCES memory.artifact(owner_user_id, artifact_id)
    ON DELETE CASCADE,
  FOREIGN KEY (owner_user_id, artifact_id, parent_section_id)
    REFERENCES memory.artifact_section(owner_user_id, artifact_id, section_id)
    ON DELETE RESTRICT,
  CHECK (ordinal >= 0),
  CHECK (level > 0),
  CHECK (btrim(content) <> ''),
  CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (char_start >= 0),
  CHECK (char_end > char_start),
  CHECK (authorship IN ('user', 'assistant', 'external', 'mixed', 'unknown'))
);

CREATE INDEX IF NOT EXISTS artifact_section_owner_artifact_idx
  ON memory.artifact_section(owner_user_id, artifact_id, ordinal);

CREATE TABLE IF NOT EXISTS memory.artifact_endorsement (
  endorsement_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  artifact_id uuid NOT NULL,
  section_id uuid,
  evidence_id uuid NOT NULL,
  endorsement_key text NOT NULL,
  endorsement_level text NOT NULL,
  explicit boolean NOT NULL DEFAULT false,
  rationale text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, endorsement_key),
  UNIQUE (owner_user_id, endorsement_id),
  FOREIGN KEY (owner_user_id, artifact_id)
    REFERENCES memory.artifact(owner_user_id, artifact_id)
    ON DELETE CASCADE,
  FOREIGN KEY (owner_user_id, artifact_id, section_id)
    REFERENCES memory.artifact_section(owner_user_id, artifact_id, section_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  CHECK (btrim(endorsement_key) <> ''),
  CHECK (
    endorsement_level IN (
      'submitted', 'reference', 'partial', 'ratified', 'rejected', 'revoked'
    )
  ),
  CHECK (endorsement_level <> 'ratified' OR explicit)
);

CREATE INDEX IF NOT EXISTS artifact_endorsement_owner_artifact_idx
  ON memory.artifact_endorsement(owner_user_id, artifact_id, created_at DESC);

CREATE INDEX IF NOT EXISTS artifact_endorsement_owner_section_idx
  ON memory.artifact_endorsement(owner_user_id, artifact_id, section_id, created_at DESC)
  WHERE section_id IS NOT NULL;

ALTER TABLE memory.candidate
  ADD COLUMN IF NOT EXISTS source_artifact_id uuid,
  ADD COLUMN IF NOT EXISTS source_section_id uuid,
  ADD COLUMN IF NOT EXISTS source_char_start integer,
  ADD COLUMN IF NOT EXISTS source_char_end integer;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'memory.candidate'::regclass
      AND conname = 'candidate_artifact_section_fk'
  ) THEN
    ALTER TABLE memory.candidate
      ADD CONSTRAINT candidate_artifact_section_fk
      FOREIGN KEY (owner_user_id, source_artifact_id, source_section_id)
      REFERENCES memory.artifact_section(owner_user_id, artifact_id, section_id)
      ON DELETE RESTRICT;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'memory.candidate'::regclass
      AND conname = 'candidate_artifact_source_shape_ck'
  ) THEN
    ALTER TABLE memory.candidate
      ADD CONSTRAINT candidate_artifact_source_shape_ck
      CHECK (
        (
          source_artifact_id IS NULL
          AND source_section_id IS NULL
          AND source_char_start IS NULL
          AND source_char_end IS NULL
        )
        OR (
          source_artifact_id IS NOT NULL
          AND source_section_id IS NOT NULL
          AND (
            (source_char_start IS NULL AND source_char_end IS NULL)
            OR (
              source_char_start IS NOT NULL
              AND source_char_end IS NOT NULL
              AND source_char_start >= 0
              AND source_char_end > source_char_start
            )
          )
        )
      );
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS candidate_owner_artifact_section_idx
  ON memory.candidate(owner_user_id, source_artifact_id, source_section_id)
  WHERE source_section_id IS NOT NULL;

DROP TRIGGER IF EXISTS artifact_set_updated_at ON memory.artifact;
CREATE TRIGGER artifact_set_updated_at
BEFORE UPDATE ON memory.artifact
FOR EACH ROW EXECUTE FUNCTION memory.set_updated_at();

REVOKE ALL ON memory.artifact FROM PUBLIC;
REVOKE ALL ON memory.artifact_occurrence FROM PUBLIC;
REVOKE ALL ON memory.artifact_section FROM PUBLIC;
REVOKE ALL ON memory.artifact_endorsement FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE
  ON memory.artifact,
     memory.artifact_occurrence,
     memory.artifact_section,
     memory.artifact_endorsement
  TO brains_app;

DO $rls$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'artifact',
    'artifact_occurrence',
    'artifact_section',
    'artifact_endorsement'
  ]
  LOOP
    EXECUTE format('ALTER TABLE memory.%I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('ALTER TABLE memory.%I FORCE ROW LEVEL SECURITY', table_name);
    EXECUTE format('DROP POLICY IF EXISTS owner_isolation ON memory.%I', table_name);
    EXECUTE format(
      'CREATE POLICY owner_isolation ON memory.%I '
      'USING (owner_user_id = memory.current_actor_user_id()) '
      'WITH CHECK (owner_user_id = memory.current_actor_user_id())',
      table_name
    );
  END LOOP;
END
$rls$;

COMMIT;
