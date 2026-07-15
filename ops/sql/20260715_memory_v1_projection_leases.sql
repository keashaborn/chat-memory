BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 migration must run as sage, current_user=%', current_user;
  END IF;
  IF to_regclass('memory.projection_outbox') IS NULL THEN
    RAISE EXCEPTION 'memory.projection_outbox does not exist';
  END IF;
END
$$;

ALTER TABLE memory.projection_outbox
  ADD COLUMN IF NOT EXISTS lease_token uuid,
  ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS worker_id text;

-- Rows claimed before leases existed cannot be completed safely. Return them
-- to the normal retry path; all other non-processing rows must have no lease.
UPDATE memory.projection_outbox
SET status='error'::memory.outbox_status,
    available_at=clock_timestamp(),
    last_error='recovered processing row during lease migration',
    lease_token=NULL,
    lease_expires_at=NULL,
    worker_id=NULL,
    updated_at=clock_timestamp()
WHERE status='processing'
  AND (lease_token IS NULL OR lease_expires_at IS NULL OR worker_id IS NULL);

UPDATE memory.projection_outbox
SET lease_token=NULL,
    lease_expires_at=NULL,
    worker_id=NULL,
    updated_at=clock_timestamp()
WHERE status <> 'processing'
  AND (lease_token IS NOT NULL OR lease_expires_at IS NOT NULL OR worker_id IS NOT NULL);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid='memory.projection_outbox'::regclass
      AND conname='projection_outbox_lease_state_check'
  ) THEN
    ALTER TABLE memory.projection_outbox
      ADD CONSTRAINT projection_outbox_lease_state_check
      CHECK (
        (
          status='processing'
          AND lease_token IS NOT NULL
          AND lease_expires_at IS NOT NULL
          AND worker_id IS NOT NULL
          AND btrim(worker_id) <> ''
          AND length(worker_id) <= 200
        )
        OR
        (
          status <> 'processing'
          AND lease_token IS NULL
          AND lease_expires_at IS NULL
          AND worker_id IS NULL
        )
      );
  END IF;
END
$$;

DROP INDEX IF EXISTS memory.projection_outbox_pending_idx;
DROP INDEX IF EXISTS memory.projection_outbox_owner_pending_idx;

CREATE INDEX IF NOT EXISTS projection_outbox_owner_ready_idx
  ON memory.projection_outbox(
    owner_user_id,
    status,
    available_at,
    lease_expires_at,
    created_at,
    outbox_id
  )
  WHERE aggregate_type='claim'
    AND status IN ('pending', 'error', 'processing');

COMMIT;
