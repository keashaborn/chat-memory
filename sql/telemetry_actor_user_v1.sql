BEGIN;

ALTER TABLE telemetry_event
  ADD COLUMN IF NOT EXISTS actor_user_id text;

CREATE INDEX IF NOT EXISTS telemetry_event_actor_user_idx
  ON telemetry_event (actor_user_id);

CREATE INDEX IF NOT EXISTS telemetry_event_actor_subject_time_idx
  ON telemetry_event (actor_user_id, subject_type, subject_id, occurred_at);

COMMIT;
