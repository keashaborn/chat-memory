BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 rollback must run as sage, current_user=%', current_user;
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.projection_outbox
    WHERE status='processing'
  ) THEN
    RAISE EXCEPTION 'cannot remove projection leases while rows are processing';
  END IF;
END
$$;

DROP INDEX IF EXISTS memory.projection_outbox_owner_ready_idx;

ALTER TABLE memory.projection_outbox
  DROP CONSTRAINT IF EXISTS projection_outbox_lease_state_check,
  DROP COLUMN IF EXISTS worker_id,
  DROP COLUMN IF EXISTS lease_expires_at,
  DROP COLUMN IF EXISTS lease_token;

CREATE INDEX IF NOT EXISTS projection_outbox_owner_pending_idx
  ON memory.projection_outbox(
    owner_user_id, status, available_at, created_at, outbox_id
  )
  WHERE aggregate_type='claim'
    AND status IN ('pending', 'error');

CREATE INDEX IF NOT EXISTS projection_outbox_pending_idx
  ON memory.projection_outbox(status, available_at, created_at)
  WHERE status IN ('pending', 'error');

COMMIT;
