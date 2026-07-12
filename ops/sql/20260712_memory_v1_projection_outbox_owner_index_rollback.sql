BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 rollback must run as sage, current_user=%', current_user;
  END IF;
END
$$;

DROP INDEX IF EXISTS memory.projection_outbox_owner_pending_idx;

COMMIT;
