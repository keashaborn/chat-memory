BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'conversation Zep sync rollback must run as sage, current_user=%',
      current_user;
  END IF;
  IF to_regclass('conversation_sync_private.zep_turn_outbox') IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM conversation_sync_private.zep_turn_outbox
     ) THEN
    RAISE EXCEPTION
      'conversation Zep sync rollback refuses to discard ledger rows';
  END IF;
END
$$;

DROP TABLE IF EXISTS conversation_sync_private.zep_turn_outbox;
DROP SCHEMA IF EXISTS conversation_sync_private;

COMMIT;
