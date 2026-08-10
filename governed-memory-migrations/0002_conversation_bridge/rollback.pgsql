-- Empty-only rollback for governed_memory_conversation_bridge_0002.
-- Run only in the canonical `memory` conversation database as `sage`. The migration
-- runner supplies BEGIN/COMMIT, timeouts, and the migration advisory lock.

DO $preflight$
BEGIN
  IF pg_catalog.current_database() <> 'memory' OR current_user <> 'sage' THEN
    RAISE EXCEPTION 'conversation bridge rollback requires sage';
  END IF;
  IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'conversation bridge rollback requires read committed';
  END IF;
  IF pg_catalog.pg_has_role(
    'brains_app', 'memory_ingest_writer', 'MEMBER'
  ) THEN
    RAISE EXCEPTION 'revoke brains_app capture membership before rollback';
  END IF;
END;
$preflight$;

-- Wait for any transaction that already entered enqueue to finish, then retain
-- this lock through the destructive statements below.  The separate DO
-- statement obtains a fresh READ COMMITTED snapshot after the wait.
LOCK TABLE memory_ingest_private.memory_ingest_outbox
  IN ACCESS EXCLUSIVE MODE;

DO $empty_only$
BEGIN
  IF EXISTS (SELECT 1 FROM memory_ingest_private.memory_ingest_outbox LIMIT 1) THEN
    RAISE EXCEPTION 'conversation bridge rollback is empty-only; rows exist';
  END IF;
END;
$empty_only$;

DROP FUNCTION memory_ingest_private.enqueue_chat_log_message(uuid,text);
DROP FUNCTION memory_ingest_private.lease_memory_ingest(
  text,integer,integer
);
DROP FUNCTION memory_ingest_private.read_leased_chat_log_message(uuid,uuid);
DROP FUNCTION memory_ingest_private.mark_memory_ingest_context_review(
  uuid,uuid
);
DROP FUNCTION memory_ingest_private.ack_memory_ingest(
  uuid,uuid,text,uuid,uuid
);
DROP FUNCTION memory_ingest_private.fail_memory_ingest(
  uuid,uuid,text,text,integer
);
DROP FUNCTION memory_ingest_private.expire_memory_ingest(integer);
DROP FUNCTION memory_ingest_private.purge_terminal_memory_ingest(integer);

DROP TABLE memory_ingest_private.memory_ingest_outbox;
DROP FUNCTION memory_ingest_private.terminal_receipt_sha256(
  text,text,text,integer,uuid,uuid,text,timestamptz
);
DROP FUNCTION memory_ingest_private.source_binding_sha256(
  uuid,uuid,uuid,uuid,uuid,integer,text,text,text,timestamptz
);
DROP FUNCTION memory_ingest_private.ingest_window_sha256(
  uuid,uuid,uuid,uuid,uuid,text
);
DROP FUNCTION memory_ingest_private.timestamp_utc_text(timestamptz);
DROP FUNCTION memory_ingest_private.framed_utf8_field(text,text);
DROP SCHEMA memory_ingest_private;
