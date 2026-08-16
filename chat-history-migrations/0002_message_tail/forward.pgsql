SET ROLE sage;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE chat_history_private.message_tail_receipt (
  owner_user_id uuid NOT NULL,
  operation_id uuid PRIMARY KEY,
  thread_id uuid NOT NULL,
  deleted_message_count integer NOT NULL CHECK (deleted_message_count >= 0),
  deleted_thread_count integer NOT NULL CHECK (deleted_thread_count >= 0),
  deleted_outbox_count integer NOT NULL CHECK (deleted_outbox_count >= 0),
  receipt_sha256 text NOT NULL CHECK (receipt_sha256 ~ '^[0-9a-f]{64}$'),
  completed_at timestamptz NOT NULL,
  UNIQUE (owner_user_id, operation_id)
);
ALTER TABLE chat_history_private.message_tail_receipt OWNER TO sage;
ALTER TABLE chat_history_private.message_tail_receipt ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_history_private.message_tail_receipt FORCE ROW LEVEL SECURITY;
REVOKE ALL ON chat_history_private.message_tail_receipt FROM PUBLIC, brains_app;
CREATE POLICY message_tail_receipt_owner_context
  ON chat_history_private.message_tail_receipt
  FOR ALL
  TO sage
  USING (
    owner_user_id = NULLIF(
      pg_catalog.current_setting('app.user_id', true), ''
    )::uuid
  )
  WITH CHECK (
    owner_user_id = NULLIF(
      pg_catalog.current_setting('app.user_id', true), ''
    )::uuid
  );
CREATE TRIGGER message_tail_receipt_immutable
  BEFORE UPDATE OR DELETE ON chat_history_private.message_tail_receipt
  FOR EACH ROW EXECUTE FUNCTION chat_history_private.guard_receipt_immutable();

CREATE FUNCTION chat_history_private.clear_message_tail(
  p_anchor_message_id uuid,
  p_thread_id uuid
)
RETURNS TABLE(
  outcome text,
  operation_id uuid,
  scope text,
  deleted_message_count integer,
  deleted_thread_count integer,
  deleted_outbox_count integer,
  receipt_sha256 text,
  completed_at timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  anchor_created_at timestamptz;
  finished_at timestamptz;
  message_count integer := 0;
  thread_count integer := 0;
  outbox_count integer := 0;
  receipt_hash text;
  existing chat_history_private.message_tail_receipt%ROWTYPE;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'brains application role required' USING ERRCODE = '42501';
  END IF;
  actor := NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid;
  IF actor IS NULL
     OR p_anchor_message_id IS NULL
     OR p_thread_id IS NULL
     OR COALESCE(
       pg_catalog.current_setting('app.auth_context_sha256', true), ''
     ) !~ '^[0-9a-f]{64}$'
  THEN
    RAISE EXCEPTION 'invalid chat history clear request'
      USING ERRCODE = '22023';
  END IF;

  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(actor::text || '|chat_history_clear', 0)
  );
  SELECT receipt.* INTO existing
  FROM chat_history_private.message_tail_receipt AS receipt
  WHERE receipt.operation_id = p_anchor_message_id;
  IF FOUND THEN
    IF existing.owner_user_id IS DISTINCT FROM actor
       OR existing.thread_id IS DISTINCT FROM p_thread_id
    THEN
      RAISE EXCEPTION 'chat history clear replay drifted' USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT 'replayed'::text, existing.operation_id,
      'message_tail'::text, existing.deleted_message_count,
      existing.deleted_thread_count, existing.deleted_outbox_count,
      existing.receipt_sha256, existing.completed_at;
    RETURN;
  END IF;

  LOCK TABLE public.threads IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE public.chat_log IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE public.chat_attachments IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE public.active_thread_selection IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE trusted_web.response_transcript_v1 IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE chat_integrity.assistant_transcript_attestation_v1
    IN SHARE ROW EXCLUSIVE MODE;
  LOCK TABLE memory_ingest_private.memory_ingest_outbox
    IN SHARE ROW EXCLUSIVE MODE;

  IF NOT EXISTS (
    SELECT 1 FROM public.threads AS thread
    WHERE thread.owner_user_id = actor AND thread.id = p_thread_id
  ) THEN
    RAISE EXCEPTION 'chat history thread is absent' USING ERRCODE = 'P0002';
  END IF;
  SELECT message.created_at INTO anchor_created_at
  FROM public.chat_log AS message
  WHERE message.owner_user_id = actor
    AND message.thread_id = p_thread_id
    AND message.id = p_anchor_message_id;
  IF NOT FOUND OR anchor_created_at IS NULL THEN
    RAISE EXCEPTION 'chat history message is absent' USING ERRCODE = 'P0002';
  END IF;
  IF EXISTS (
    SELECT 1 FROM public.chat_log AS message
    WHERE message.owner_user_id = actor
      AND message.thread_id = p_thread_id
      AND message.created_at IS NULL
  ) THEN
    RAISE EXCEPTION 'invalid chat history clear request' USING ERRCODE = '22023';
  END IF;

  CREATE TEMP TABLE pg_temp.chat_history_target_message (
    message_id uuid PRIMARY KEY,
    thread_id uuid NOT NULL
  ) ON COMMIT DROP;
  INSERT INTO pg_temp.chat_history_target_message(message_id, thread_id)
  SELECT message.id, message.thread_id
  FROM public.chat_log AS message
  WHERE message.owner_user_id = actor
    AND message.thread_id = p_thread_id
    AND (message.created_at, message.id) >= (
      anchor_created_at, p_anchor_message_id
    );

  IF EXISTS (
    SELECT 1
    FROM memory_ingest_private.memory_ingest_outbox AS outbox
    JOIN pg_temp.chat_history_target_message AS target
      ON target.message_id = outbox.message_id
    WHERE outbox.owner_user_id = actor
      AND outbox.state NOT IN (
        'completed', 'failed_terminal', 'skipped', 'expired'
      )
  ) THEN
    RAISE EXCEPTION 'chat memory is still processing' USING ERRCODE = '55000';
  END IF;

  DELETE FROM memory_ingest_private.memory_ingest_outbox AS outbox
  USING pg_temp.chat_history_target_message AS target
  WHERE outbox.owner_user_id = actor
    AND outbox.message_id = target.message_id;
  GET DIAGNOSTICS outbox_count = ROW_COUNT;

  DELETE FROM public.chat_log AS message
  USING pg_temp.chat_history_target_message AS target
  WHERE message.owner_user_id = actor
    AND message.id = target.message_id;
  GET DIAGNOSTICS message_count = ROW_COUNT;

  DELETE FROM public.threads AS thread
  WHERE thread.owner_user_id = actor
    AND thread.id = p_thread_id
    AND NOT EXISTS (
      SELECT 1 FROM public.chat_log AS remaining
      WHERE remaining.owner_user_id = actor
        AND remaining.thread_id = thread.id
    );
  GET DIAGNOSTICS thread_count = ROW_COUNT;

  finished_at := pg_catalog.clock_timestamp();
  receipt_hash := pg_catalog.encode(
    pg_catalog.sha256(pg_catalog.convert_to(
      actor::text || '|' || p_anchor_message_id::text || '|message_tail|'
      || p_thread_id::text || '|' || message_count::text || '|'
      || thread_count::text || '|' || outbox_count::text || '|'
      || finished_at::text,
      'UTF8'
    )),
    'hex'
  );
  INSERT INTO chat_history_private.message_tail_receipt(
    owner_user_id, operation_id, thread_id, deleted_message_count,
    deleted_thread_count, deleted_outbox_count, receipt_sha256, completed_at
  ) VALUES (
    actor, p_anchor_message_id, p_thread_id, message_count,
    thread_count, outbox_count, receipt_hash, finished_at
  );

  RETURN QUERY SELECT 'cleared'::text, p_anchor_message_id,
    'message_tail'::text, message_count, thread_count, outbox_count,
    receipt_hash, finished_at;
END;
$function$;
ALTER FUNCTION chat_history_private.clear_message_tail(uuid, uuid)
  OWNER TO sage;
REVOKE ALL ON FUNCTION chat_history_private.clear_message_tail(uuid, uuid)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION chat_history_private.clear_message_tail(uuid, uuid)
  TO brains_app;

DO $postflight$
BEGIN
  IF pg_catalog.has_table_privilege(
       'brains_app', 'chat_history_private.message_tail_receipt',
       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
     )
     OR NOT pg_catalog.has_function_privilege(
       'brains_app',
       'chat_history_private.clear_message_tail(uuid,uuid)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'message tail clear privilege postflight failed';
  END IF;
END;
$postflight$;

RESET ROLE;
