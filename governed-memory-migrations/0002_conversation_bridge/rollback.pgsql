-- Empty-only rollback for governed_memory_conversation_bridge_0002.
-- Run only in the existing conversation database as `sage`. The migration
-- runner supplies BEGIN/COMMIT, timeouts, and the migration advisory lock.

DO $preflight$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'conversation bridge rollback requires sage';
  END IF;
  IF EXISTS (SELECT 1 FROM public.memory_ingest_outbox LIMIT 1) THEN
    RAISE EXCEPTION 'conversation bridge rollback is empty-only; rows exist';
  END IF;
END;
$preflight$;

DROP FUNCTION memory_ingest_private.enqueue_memory_ingest(
  uuid,uuid,uuid,text,timestamptz,uuid,uuid,integer,text,text
);
DROP FUNCTION memory_ingest_private.lease_memory_ingest(
  text,integer,integer
);
DROP FUNCTION memory_ingest_private.read_memory_ingest_lease(uuid,uuid);
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

DROP TABLE public.memory_ingest_outbox;
DROP FUNCTION memory_ingest_private.terminal_receipt_sha256(
  text,text,text,integer,uuid,uuid,text,timestamptz
);
DROP FUNCTION memory_ingest_private.source_binding_sha256(
  uuid,uuid,uuid,uuid,uuid,integer,text,text,text,timestamptz
);
DROP FUNCTION memory_ingest_private.timestamp_utc_text(timestamptz);
DROP FUNCTION memory_ingest_private.framed_utf8_field(text,text);
DROP SCHEMA memory_ingest_private;
