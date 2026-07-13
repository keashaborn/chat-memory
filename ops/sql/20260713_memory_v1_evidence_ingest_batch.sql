BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 evidence ingest batch migration must run as sage, current_user=%',
      current_user;
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS memory.evidence_ingest_batch (
  batch_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  batch_key text NOT NULL,
  manifest_version text NOT NULL,
  plan_version text NOT NULL,
  input_fingerprint_sha256 text NOT NULL,
  reviewed_report_sha256 text NOT NULL,
  authorization_manifest_sha256 text NOT NULL,
  source_snapshot_sha256 text NOT NULL,
  source_row_count integer NOT NULL,
  expected_evidence_count integer NOT NULL,
  inserted_count integer NOT NULL,
  reused_count integer NOT NULL,
  status text NOT NULL DEFAULT 'committed',
  actor_user_id uuid NOT NULL,
  invoked_by_role name NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, batch_id),
  UNIQUE (owner_user_id, batch_key),
  CHECK (batch_id <> '00000000-0000-0000-0000-000000000000'::uuid),
  CHECK (owner_user_id <> '00000000-0000-0000-0000-000000000000'::uuid),
  CHECK (btrim(batch_key) <> ''),
  CHECK (btrim(manifest_version) <> ''),
  CHECK (btrim(plan_version) <> ''),
  CHECK (input_fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (reviewed_report_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (authorization_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (source_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (source_row_count > 0),
  CHECK (expected_evidence_count > 0),
  CHECK (inserted_count >= 0),
  CHECK (reused_count >= 0),
  CHECK (inserted_count + reused_count = expected_evidence_count),
  CHECK (status = 'committed'),
  CHECK (actor_user_id = owner_user_id),
  CHECK (invoked_by_role = 'brains_app'::name),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (pg_column_size(metadata) <= 16384)
);

CREATE INDEX IF NOT EXISTS evidence_ingest_batch_owner_time_idx
  ON memory.evidence_ingest_batch(owner_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory.evidence_ingest_batch_row (
  owner_user_id uuid NOT NULL,
  batch_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  external_id text NOT NULL,
  content_sha256 text NOT NULL,
  operation text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, batch_id, evidence_id),
  UNIQUE (owner_user_id, batch_id, external_id),
  FOREIGN KEY (owner_user_id, batch_id)
    REFERENCES memory.evidence_ingest_batch(owner_user_id, batch_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  CHECK (btrim(external_id) <> ''),
  CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (operation IN ('inserted', 'reused'))
);

CREATE INDEX IF NOT EXISTS evidence_ingest_batch_row_owner_evidence_idx
  ON memory.evidence_ingest_batch_row(owner_user_id, evidence_id);

CREATE OR REPLACE FUNCTION memory.guard_evidence_ingest_audit_append_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
  RAISE EXCEPTION 'evidence ingest audit rows are append-only'
    USING ERRCODE = '42501';
END
$$;

DROP TRIGGER IF EXISTS evidence_ingest_batch_append_only_guard
  ON memory.evidence_ingest_batch;
CREATE TRIGGER evidence_ingest_batch_append_only_guard
BEFORE UPDATE OR DELETE ON memory.evidence_ingest_batch
FOR EACH ROW EXECUTE FUNCTION memory.guard_evidence_ingest_audit_append_only();

DROP TRIGGER IF EXISTS evidence_ingest_batch_row_append_only_guard
  ON memory.evidence_ingest_batch_row;
CREATE TRIGGER evidence_ingest_batch_row_append_only_guard
BEFORE UPDATE OR DELETE ON memory.evidence_ingest_batch_row
FOR EACH ROW EXECUTE FUNCTION memory.guard_evidence_ingest_audit_append_only();

REVOKE ALL ON memory.evidence_ingest_batch FROM PUBLIC;
REVOKE ALL ON memory.evidence_ingest_batch_row FROM PUBLIC;
REVOKE ALL ON memory.evidence_ingest_batch FROM brains_app;
REVOKE ALL ON memory.evidence_ingest_batch_row FROM brains_app;
GRANT SELECT, INSERT ON memory.evidence_ingest_batch TO brains_app;
GRANT SELECT, INSERT ON memory.evidence_ingest_batch_row TO brains_app;
REVOKE ALL ON FUNCTION memory.guard_evidence_ingest_audit_append_only()
  FROM PUBLIC;

ALTER TABLE memory.evidence_ingest_batch ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.evidence_ingest_batch FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.evidence_ingest_batch;
CREATE POLICY owner_isolation ON memory.evidence_ingest_batch
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

ALTER TABLE memory.evidence_ingest_batch_row ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.evidence_ingest_batch_row FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.evidence_ingest_batch_row;
CREATE POLICY owner_isolation ON memory.evidence_ingest_batch_row
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

COMMIT;
