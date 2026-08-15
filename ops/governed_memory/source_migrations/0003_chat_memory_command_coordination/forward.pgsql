\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';

DO $preflight$
BEGIN
  IF current_database() <> 'memory' OR current_user <> 'sage' THEN
    RAISE EXCEPTION 'chat memory command coordination identity mismatch';
  END IF;
  IF to_regclass('memory_ingest_private.memory_ingest_outbox') IS NULL
     OR to_regclass('public.chat_log') IS NULL
     OR to_regprocedure(
       'memory_ingest_private.terminal_receipt_sha256(text,text,text,integer,uuid,uuid,text,timestamp with time zone)'
     ) IS NULL
     OR NOT EXISTS (
       SELECT 1 FROM pg_roles WHERE rolname = 'memory_ingest_writer'
     ) THEN
    RAISE EXCEPTION 'chat memory command coordination prerequisite absent';
  END IF;
  IF to_regprocedure(
       'memory_ingest_private.resolve_chat_memory_command(uuid,text,uuid,text,text)'
     ) IS NOT NULL
     OR EXISTS (
       SELECT 1
       FROM pg_trigger
       WHERE tgrelid =
         'memory_ingest_private.memory_ingest_outbox'::regclass
         AND tgname = 'defer_chat_memory_ingest_until_response'
         AND NOT tgisinternal
     ) THEN
    RAISE EXCEPTION 'chat memory command coordination already exists';
  END IF;
END;
$preflight$;

CREATE FUNCTION memory_ingest_private.defer_chat_memory_ingest_until_response()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  IF NEW.state <> 'pending'
     OR NEW.available_at IS NULL
     OR NEW.created_at IS NULL THEN
    RAISE EXCEPTION 'invalid new memory ingest coordination row'
      USING ERRCODE = '23514';
  END IF;
  NEW.available_at := GREATEST(
    NEW.available_at,
    NEW.created_at + interval '120 seconds'
  );
  RETURN NEW;
END;
$function$;
ALTER FUNCTION
  memory_ingest_private.defer_chat_memory_ingest_until_response()
  OWNER TO sage;
REVOKE ALL ON FUNCTION
  memory_ingest_private.defer_chat_memory_ingest_until_response()
  FROM PUBLIC, memory_ingest_writer, governed_memory_worker;

CREATE TRIGGER defer_chat_memory_ingest_until_response
  BEFORE INSERT ON memory_ingest_private.memory_ingest_outbox
  FOR EACH ROW EXECUTE FUNCTION
    memory_ingest_private.defer_chat_memory_ingest_until_response();

CREATE FUNCTION memory_ingest_private.resolve_chat_memory_command(
  p_message_id uuid,
  p_request_id text,
  p_thread_id uuid,
  p_content_sha256 text,
  p_resolution text
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  source_row public.chat_log%ROWTYPE;
  target memory_ingest_private.memory_ingest_outbox%ROWTYPE;
  captured_at timestamptz;
  receipt_hash text;
BEGIN
  IF session_user <> 'brains_app'
     OR NOT pg_catalog.pg_has_role(
       session_user, 'memory_ingest_writer', 'MEMBER'
     ) THEN
    RAISE EXCEPTION 'authorized brains_app coordination membership required'
      USING ERRCODE = '42501';
  END IF;
  actor := NULLIF(
    pg_catalog.current_setting('app.user_id', true), ''
  )::uuid;
  IF actor IS NULL OR (p_message_id IS NULL AND p_request_id IS NULL)
     OR p_thread_id IS NULL
     OR (p_request_id IS NOT NULL AND (
       p_request_id <> pg_catalog.btrim(p_request_id)
       OR pg_catalog.length(p_request_id) NOT BETWEEN 1 AND 128
     ))
     OR p_content_sha256 IS NULL
     OR p_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_resolution IS NULL
     OR p_resolution NOT IN ('validate', 'release', 'suppress') THEN
    RAISE EXCEPTION 'invalid chat memory command coordination input'
      USING ERRCODE = '22023';
  END IF;

  BEGIN
    SELECT source.* INTO STRICT source_row
    FROM public.chat_log AS source
    WHERE source.owner_user_id = actor
      AND source.thread_id = p_thread_id
      AND source.source = 'frontend/chat:user'
      AND source.text IS NOT NULL
      AND (p_message_id IS NULL OR source.id = p_message_id)
      AND (p_request_id IS NULL OR source.request_id = p_request_id)
      AND pg_catalog.encode(pg_catalog.sha256(
        pg_catalog.convert_to(source.text, 'UTF8')
      ), 'hex') = p_content_sha256
    FOR KEY SHARE;
  EXCEPTION
    WHEN NO_DATA_FOUND OR TOO_MANY_ROWS THEN
      RAISE EXCEPTION 'chat memory command source binding mismatch'
        USING ERRCODE = '23514';
  END;

  SELECT value.* INTO target
  FROM memory_ingest_private.memory_ingest_outbox AS value
  WHERE value.owner_user_id = actor
    AND value.message_id = source_row.id
    AND value.thread_id = p_thread_id
  FOR UPDATE;
  IF NOT FOUND THEN
    RETURN 'absent';
  END IF;
  IF target.content_sha256 IS NOT NULL
     AND target.content_sha256 <> p_content_sha256 THEN
    RAISE EXCEPTION 'chat memory command outbox binding mismatch'
      USING ERRCODE = '23514';
  END IF;

  IF p_resolution = 'validate' THEN
    RETURN 'validated';
  END IF;

  IF p_resolution = 'release' THEN
    IF target.state IN ('pending', 'retryable') THEN
      UPDATE memory_ingest_private.memory_ingest_outbox AS value
      SET available_at = pg_catalog.clock_timestamp(),
          updated_at = pg_catalog.clock_timestamp()
      WHERE value.owner_user_id = actor
        AND value.outbox_id = target.outbox_id;
      RETURN 'released';
    END IF;
    IF target.state = 'skipped'
       AND target.eligibility_decision = 'skip_zero_call' THEN
      RETURN 'replayed';
    END IF;
    RAISE EXCEPTION 'chat memory ingest already resolved or claimed'
      USING ERRCODE = '40001';
  END IF;

  IF target.state = 'skipped'
     AND target.eligibility_decision = 'skip_zero_call'
     AND target.last_error_code IS NULL THEN
    RETURN 'replayed';
  END IF;
  IF target.state NOT IN ('pending', 'retryable') THEN
    RAISE EXCEPTION 'chat memory ingest already resolved or claimed'
      USING ERRCODE = '40001';
  END IF;
  captured_at := pg_catalog.transaction_timestamp();
  receipt_hash := memory_ingest_private.terminal_receipt_sha256(
    target.source_binding_sha256,
    'skipped',
    'skip_zero_call',
    target.context_review_count,
    NULL::uuid,
    NULL::uuid,
    NULL::text,
    captured_at
  );
  UPDATE memory_ingest_private.memory_ingest_outbox AS value
  SET state = 'skipped', eligibility_decision = 'skip_zero_call',
      content_sha256 = NULL, completed_at = captured_at,
      terminal_receipt_sha256 = receipt_hash,
      lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
      lease_expires_at = NULL, last_error_code = NULL,
      updated_at = captured_at
  WHERE value.owner_user_id = actor
    AND value.outbox_id = target.outbox_id;
  RETURN 'suppressed';
END;
$function$;
ALTER FUNCTION memory_ingest_private.resolve_chat_memory_command(
  uuid,text,uuid,text,text
) OWNER TO sage;
REVOKE ALL ON FUNCTION memory_ingest_private.resolve_chat_memory_command(
  uuid,text,uuid,text,text
) FROM PUBLIC, memory_ingest_writer, governed_memory_worker;
GRANT EXECUTE ON FUNCTION
  memory_ingest_private.resolve_chat_memory_command(uuid,text,uuid,text,text)
  TO memory_ingest_writer;

DO $postflight$
BEGIN
  IF to_regprocedure(
       'memory_ingest_private.resolve_chat_memory_command(uuid,text,uuid,text,text)'
     ) IS NULL
     OR NOT pg_catalog.has_function_privilege(
       'memory_ingest_writer',
       'memory_ingest_private.resolve_chat_memory_command(uuid,text,uuid,text,text)',
       'EXECUTE'
     )
     OR NOT EXISTS (
       SELECT 1
       FROM pg_trigger
       WHERE tgrelid =
         'memory_ingest_private.memory_ingest_outbox'::regclass
         AND tgname = 'defer_chat_memory_ingest_until_response'
         AND NOT tgisinternal
         AND tgenabled = 'O'
     ) THEN
    RAISE EXCEPTION 'chat memory command coordination postflight failed';
  END IF;
END;
$postflight$;

COMMIT;
