BEGIN;

-- For UUID generation / compatibility (safe if already installed)
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS telemetry_event (
  event_id uuid PRIMARY KEY,
  event_type text NOT NULL,

  subject_type text NOT NULL,
  subject_id text NOT NULL,

  target_model_id text,
  target_model_version text,
  judge_model_id text,
  judge_model_version text,

  vantage_id text,
  condition_id text,
  thread_id text,
  turn_id text,

  payload jsonb NOT NULL DEFAULT '{}'::jsonb,

  occurred_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS telemetry_event_occurred_at_idx ON telemetry_event (occurred_at);
CREATE INDEX IF NOT EXISTS telemetry_event_event_type_idx  ON telemetry_event (event_type);
CREATE INDEX IF NOT EXISTS telemetry_event_subject_idx     ON telemetry_event (subject_type, subject_id);
CREATE INDEX IF NOT EXISTS telemetry_event_target_model_idx ON telemetry_event (target_model_id);
CREATE INDEX IF NOT EXISTS telemetry_event_condition_idx   ON telemetry_event (condition_id);

CREATE INDEX IF NOT EXISTS telemetry_event_payload_gin_idx
  ON telemetry_event
  USING gin (payload jsonb_path_ops);

COMMIT;
