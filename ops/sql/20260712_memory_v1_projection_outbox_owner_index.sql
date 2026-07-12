BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 migration must run as sage, current_user=%', current_user;
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS projection_outbox_owner_pending_idx
  ON memory.projection_outbox(
    owner_user_id, status, available_at, created_at, outbox_id
  )
  WHERE aggregate_type = 'claim'
    AND status IN ('pending', 'error');

COMMIT;
