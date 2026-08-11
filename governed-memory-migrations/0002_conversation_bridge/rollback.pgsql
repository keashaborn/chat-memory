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
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_auth_members AS membership
    JOIN pg_catalog.pg_roles AS granted_role
      ON granted_role.oid = membership.roleid
    JOIN pg_catalog.pg_roles AS member_role
      ON member_role.oid = membership.member
    WHERE granted_role.rolname IN (
      'governed_memory_api',
      'governed_memory_worker',
      'memory_ingest_writer',
      'memory_erasure_requester'
    ) OR member_role.rolname IN (
      'governed_memory_api',
      'governed_memory_worker',
      'memory_ingest_writer',
      'memory_erasure_requester'
    )
  ) THEN
    RAISE EXCEPTION
      'source runtime membership graph must be empty before rollback';
  END IF;
END;
$preflight$;

-- Serialize with ordinary chat inserts before removing the erasure trigger,
-- then wait for bridge and coordinator calls that already entered a function.
-- The separate DO statement gets a fresh READ COMMITTED snapshot after waits.
LOCK TABLE public.threads IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.chat_log IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.chat_attachments IN ACCESS EXCLUSIVE MODE;
DO $catalog_lock$
BEGIN
  IF pg_catalog.to_regclass('public.active_thread_selection') IS NOT NULL THEN
    EXECUTE
      'LOCK TABLE public.active_thread_selection IN ACCESS EXCLUSIVE MODE';
  END IF;
  IF pg_catalog.to_regclass(
       'trusted_web.response_transcript_v1'
     ) IS NOT NULL THEN
    EXECUTE
      'LOCK TABLE trusted_web.response_transcript_v1 IN ACCESS EXCLUSIVE MODE';
  END IF;
END;
$catalog_lock$;
LOCK TABLE memory_ingest_private.memory_ingest_outbox
  IN ACCESS EXCLUSIVE MODE;
LOCK TABLE memory_ingest_private.source_erasure_operation
  IN ACCESS EXCLUSIVE MODE;
LOCK TABLE memory_ingest_private.source_erasure_target
  IN ACCESS EXCLUSIVE MODE;
LOCK TABLE memory_ingest_private.source_erasure_thread_target
  IN ACCESS EXCLUSIVE MODE;
LOCK TABLE memory_ingest_private.source_erasure_message_tombstone
  IN ACCESS EXCLUSIVE MODE;
LOCK TABLE memory_ingest_private.source_erasure_thread_tombstone
  IN ACCESS EXCLUSIVE MODE;
LOCK TABLE memory_ingest_private.source_erasure_receipt
  IN ACCESS EXCLUSIVE MODE;

DO $catalog_assert$
BEGIN
  PERFORM memory_ingest_private.assert_chat_deletion_catalog();
END;
$catalog_assert$;

DO $empty_only$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_operation
    LIMIT 1
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_target
    LIMIT 1
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_target
    LIMIT 1
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_message_tombstone
    LIMIT 1
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_thread_tombstone
    LIMIT 1
  ) OR EXISTS (
    SELECT 1
    FROM memory_ingest_private.source_erasure_receipt
    LIMIT 1
  ) THEN
    RAISE EXCEPTION 'conversation bridge rollback is empty-only; erasure rows exist';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.memory_ingest_outbox
    WHERE state = 'erasure_cancelled'
    LIMIT 1
  ) THEN
    RAISE EXCEPTION 'conversation bridge rollback blocked by erasure outbox state';
  END IF;
  IF EXISTS (SELECT 1 FROM memory_ingest_private.memory_ingest_outbox LIMIT 1) THEN
    RAISE EXCEPTION 'conversation bridge rollback is empty-only; rows exist';
  END IF;
END;
$empty_only$;

DROP TRIGGER source_erasure_thread_tombstone_immutable
  ON memory_ingest_private.source_erasure_thread_tombstone;
DROP TRIGGER source_erasure_message_tombstone_immutable
  ON memory_ingest_private.source_erasure_message_tombstone;
DROP TRIGGER source_erasure_receipt_immutable
  ON memory_ingest_private.source_erasure_receipt;
DROP TRIGGER chat_attachments_serialize_source_erasure
  ON public.chat_attachments;
DO $drop_response_transcript_trigger$
BEGIN
  IF pg_catalog.to_regclass(
       'trusted_web.response_transcript_v1'
     ) IS NOT NULL THEN
    EXECUTE
      'DROP TRIGGER response_transcript_serialize_source_erasure '
      'ON trusted_web.response_transcript_v1';
  END IF;
END;
$drop_response_transcript_trigger$;
DROP TRIGGER threads_serialize_source_erasure ON public.threads;
DROP TRIGGER chat_log_serialize_source_erasure ON public.chat_log;
DROP FUNCTION memory_ingest_private.serialize_attachment_source_erasure();
DROP FUNCTION
  memory_ingest_private.serialize_response_transcript_source_erasure();
DROP FUNCTION memory_ingest_private.serialize_thread_source_erasure();
DROP FUNCTION memory_ingest_private.serialize_chat_source_erasure();
DROP FUNCTION
  memory_ingest_private.guard_source_erasure_receipt_immutable();
DROP FUNCTION memory_ingest_private.begin_source_erasure(
  uuid,text,uuid,uuid,integer,text
);
DROP FUNCTION memory_ingest_private.read_source_erasure(uuid);
DROP FUNCTION memory_ingest_private.lease_source_erasure(text,integer);
DROP FUNCTION memory_ingest_private.read_source_erasure_targets(
  uuid,uuid,timestamptz,uuid,integer
);
DROP FUNCTION memory_ingest_private.release_source_erasure_lease(uuid,uuid);
DROP FUNCTION memory_ingest_private.mark_source_erasure_governed_deleted(
  uuid,uuid,text,integer,text
);
DROP FUNCTION memory_ingest_private.finalize_source_erasure(uuid,uuid,text);
DROP FUNCTION memory_ingest_private.ack_source_erasure_completion(
  uuid,uuid,text
);
DROP FUNCTION memory_ingest_private.fail_source_erasure(uuid,uuid,text,text);

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

DROP TABLE memory_ingest_private.source_erasure_thread_target;
DROP TABLE memory_ingest_private.source_erasure_target;
DROP TABLE memory_ingest_private.source_erasure_message_tombstone;
DROP TABLE memory_ingest_private.source_erasure_thread_tombstone;
DROP TABLE memory_ingest_private.source_erasure_receipt;
DROP TABLE memory_ingest_private.source_erasure_operation;
DROP TABLE memory_ingest_private.memory_ingest_outbox;
DROP FUNCTION memory_ingest_private.source_erasure_target_sha256(
  uuid,uuid,uuid,uuid,timestamptz
);
DROP FUNCTION memory_ingest_private.terminal_receipt_sha256(
  text,text,text,integer,uuid,uuid,text,timestamptz
);
DROP FUNCTION memory_ingest_private.source_binding_sha256(
  uuid,uuid,uuid,uuid,uuid,integer,text,text,text,timestamptz
);
DROP FUNCTION memory_ingest_private.ingest_window_sha256(
  uuid,uuid,uuid,uuid,uuid,text
);
DROP FUNCTION memory_ingest_private.assert_chat_deletion_catalog();
DROP FUNCTION memory_ingest_private.timestamp_utc_text(timestamptz);
DROP FUNCTION memory_ingest_private.framed_utf8_field(text,text);
DROP SCHEMA memory_ingest_private;

DO $postflight$
BEGIN
  IF pg_catalog.to_regnamespace('memory_ingest_private') IS NOT NULL
     OR pg_catalog.to_regprocedure(
          'memory_ingest_private.assert_chat_deletion_catalog()'
        ) IS NOT NULL
     OR EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger
    WHERE tgname IN (
        'chat_log_serialize_source_erasure',
        'threads_serialize_source_erasure',
        'chat_attachments_serialize_source_erasure',
        'response_transcript_serialize_source_erasure'
      )
      AND tgrelid IN (
        'public.chat_log'::regclass,
        'public.threads'::regclass,
        'public.chat_attachments'::regclass,
        pg_catalog.to_regclass('trusted_web.response_transcript_v1')
      )
      AND NOT tgisinternal
  ) THEN
    RAISE EXCEPTION 'conversation bridge rollback left private objects';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_trigger
    WHERE tgrelid = 'public.chat_log'::regclass
      AND tgname = 'chat_log_enqueue_memory_v1_consolidation'
      AND NOT tgisinternal
      AND (
        tgenabled <> 'D'
        OR tgtype <> 5
        OR tgfoid IS DISTINCT FROM pg_catalog.to_regprocedure(
             'memory.enqueue_chat_log_consolidation()'
           )
      )
  ) THEN
    RAISE EXCEPTION 'legacy chat capture trigger changed during rollback';
  END IF;
END;
$postflight$;
