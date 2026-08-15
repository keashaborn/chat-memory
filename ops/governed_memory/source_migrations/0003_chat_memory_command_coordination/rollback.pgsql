\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';

DO $preflight$
BEGIN
  IF current_database() <> 'memory' OR current_user <> 'sage' THEN
    RAISE EXCEPTION 'chat memory command coordination rollback identity mismatch';
  END IF;
END;
$preflight$;

DROP FUNCTION memory_ingest_private.resolve_chat_memory_command(
  uuid,uuid,text,text
);
DROP TRIGGER defer_chat_memory_ingest_until_response
  ON memory_ingest_private.memory_ingest_outbox;
DROP FUNCTION
  memory_ingest_private.defer_chat_memory_ingest_until_response();

COMMIT;
