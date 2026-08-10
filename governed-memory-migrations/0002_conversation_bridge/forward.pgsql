-- Content-free conversation-to-Memory bridge. Apply to the existing
-- conversation database only. This migration does not inspect or depend on the
-- shape of any conversation, thread, attachment, account, or legacy table.

DO $preflight$
BEGIN
  IF pg_catalog.current_setting('server_version_num')::integer < 150000 THEN
    RAISE EXCEPTION 'PostgreSQL 15 or newer is required';
  END IF;
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'conversation bridge migration requires sage';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles
    WHERE rolname = 'memory_ingest_writer'
      AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
      AND NOT rolinherit
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles
    WHERE rolname = 'governed_memory_worker'
      AND rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
      AND NOT rolinherit
  ) THEN
    RAISE EXCEPTION 'bridge runtime roles are absent or unsafe';
  END IF;
END;
$preflight$;

CREATE SCHEMA memory_ingest_private AUTHORIZATION sage;
REVOKE ALL ON SCHEMA memory_ingest_private FROM PUBLIC;
GRANT USAGE ON SCHEMA memory_ingest_private
  TO memory_ingest_writer, governed_memory_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE sage IN SCHEMA memory_ingest_private
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE FUNCTION memory_ingest_private.framed_utf8_field(
  p_name text,
  p_value text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT p_name || ':' || CASE
    WHEN p_value IS NULL THEN '-:' || E'\n'
    ELSE pg_catalog.octet_length(
           pg_catalog.convert_to(normalize(p_value, NFC), 'UTF8')
         )::text
         || ':' || normalize(p_value, NFC) || E'\n'
  END
$function$;
REVOKE ALL ON FUNCTION memory_ingest_private.framed_utf8_field(text,text)
  FROM PUBLIC;

CREATE FUNCTION memory_ingest_private.timestamp_utc_text(p_value timestamptz)
RETURNS text
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT CASE WHEN p_value IS NULL THEN NULL::text ELSE pg_catalog.to_char(
    p_value AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
  ) END
$function$;
REVOKE ALL ON FUNCTION memory_ingest_private.timestamp_utc_text(timestamptz)
  FROM PUBLIC;

CREATE FUNCTION memory_ingest_private.source_binding_sha256(
  p_owner_user_id uuid,
  p_message_id uuid,
  p_thread_id uuid,
  p_exchange_id uuid,
  p_window_id uuid,
  p_window_ordinal integer,
  p_window_sha256 text,
  p_content_sha256 text,
  p_policy_sha256 text,
  p_source_created_at timestamptz
)
RETURNS text
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.bridge_source.v1' || E'\n'
      || memory_ingest_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'message_id', p_message_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'thread_id', p_thread_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'exchange_id', p_exchange_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'window_id', p_window_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'window_ordinal', p_window_ordinal::text
         )
      || memory_ingest_private.framed_utf8_field(
           'window_sha256', p_window_sha256
         )
      || memory_ingest_private.framed_utf8_field(
           'content_sha256', p_content_sha256
         )
      || memory_ingest_private.framed_utf8_field(
           'policy_sha256', p_policy_sha256
         )
      || memory_ingest_private.framed_utf8_field(
           'source_created_at',
           memory_ingest_private.timestamp_utc_text(p_source_created_at)
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_ingest_private.source_binding_sha256(
  uuid,uuid,uuid,uuid,uuid,integer,text,text,text,timestamptz
) FROM PUBLIC;

DO $source_binding_hash_self_check$
BEGIN
  IF memory_ingest_private.source_binding_sha256(
       '00000000-0000-4000-8000-000000000001'::uuid,
       '00000000-0000-4000-8000-000000000002'::uuid,
       '00000000-0000-4000-8000-000000000003'::uuid,
       '00000000-0000-4000-8000-000000000004'::uuid,
       '00000000-0000-4000-8000-000000000005'::uuid,
       7, pg_catalog.repeat('1', 64), pg_catalog.repeat('2', 64),
       pg_catalog.repeat('3', 64),
       '2026-08-09 12:34:56.123456+00'::timestamptz
     ) <> 'f1ed425554af394ea4a0b12cfcbf5dbd069b36131a55cb5411b3fc34d49ec279'
  THEN
    RAISE EXCEPTION 'bridge source binding hash self-check failed';
  END IF;
END;
$source_binding_hash_self_check$;

CREATE FUNCTION memory_ingest_private.terminal_receipt_sha256(
  p_source_binding_sha256 text,
  p_state text,
  p_decision text,
  p_context_review_count integer,
  p_successor_evidence_id uuid,
  p_successor_job_id uuid,
  p_error_code text,
  p_completed_at timestamptz
)
RETURNS text
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.conversation_terminal_receipt.v1' || E'\n'
      || memory_ingest_private.framed_utf8_field(
           'source_binding_sha256', p_source_binding_sha256
         )
      || memory_ingest_private.framed_utf8_field('state', p_state)
      || memory_ingest_private.framed_utf8_field('decision', p_decision)
      || memory_ingest_private.framed_utf8_field(
           'context_review_count', p_context_review_count::text
         )
      || memory_ingest_private.framed_utf8_field(
           'successor_evidence_id', p_successor_evidence_id::text
         )
      || memory_ingest_private.framed_utf8_field(
           'successor_job_id', p_successor_job_id::text
         )
      || memory_ingest_private.framed_utf8_field('error_code', p_error_code)
      || memory_ingest_private.framed_utf8_field(
           'completed_at',
           memory_ingest_private.timestamp_utc_text(p_completed_at)
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_ingest_private.terminal_receipt_sha256(
  text,text,text,integer,uuid,uuid,text,timestamptz
) FROM PUBLIC;

CREATE TABLE public.memory_ingest_outbox (
  outbox_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  message_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  exchange_id uuid NOT NULL,
  window_id uuid NOT NULL,
  window_ordinal integer NOT NULL,
  window_sha256 text NOT NULL,
  content_sha256 text,
  source_binding_sha256 text NOT NULL,
  ingest_after timestamptz NOT NULL,
  source_created_at timestamptz NOT NULL,
  policy_sha256 text NOT NULL,
  state text NOT NULL DEFAULT 'pending',
  eligibility_decision text,
  context_review_count integer NOT NULL DEFAULT 0,
  attempt_count integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3,
  available_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  lease_token uuid,
  claimed_by text,
  claimed_at timestamptz,
  lease_expires_at timestamptz,
  successor_evidence_id uuid,
  successor_job_id uuid,
  last_error_code text,
  content_hash_expires_at timestamptz NOT NULL,
  purge_after timestamptz NOT NULL,
  completed_at timestamptz,
  terminal_receipt_sha256 text,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT memory_ingest_outbox_owner_id UNIQUE (owner_user_id, outbox_id),
  CONSTRAINT memory_ingest_outbox_operation_unique UNIQUE (
    owner_user_id, operation_id
  ),
  CONSTRAINT memory_ingest_outbox_message_unique UNIQUE (
    owner_user_id, message_id
  ),
  CONSTRAINT memory_ingest_outbox_owner_nonzero CHECK (
    owner_user_id <> '00000000-0000-0000-0000-000000000000'::uuid
  ),
  CONSTRAINT memory_ingest_outbox_hashes CHECK (
    (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$')
    AND source_binding_sha256 ~ '^[0-9a-f]{64}$'
    AND window_sha256 ~ '^[0-9a-f]{64}$'
    AND policy_sha256 ~ '^[0-9a-f]{64}$'
    AND (
      terminal_receipt_sha256 IS NULL
      OR terminal_receipt_sha256 ~ '^[0-9a-f]{64}$'
    )
  ),
  CONSTRAINT memory_ingest_outbox_cutover CHECK (
    source_created_at >= ingest_after
    AND content_hash_expires_at > created_at
    AND content_hash_expires_at <= created_at + interval '24 hours'
    AND purge_after > content_hash_expires_at
    AND purge_after <= created_at + interval '31 days'
  ),
  CONSTRAINT memory_ingest_outbox_window CHECK (
    window_id <> '00000000-0000-0000-0000-000000000000'::uuid
    AND window_ordinal BETWEEN 0 AND 10000
  ),
  CONSTRAINT memory_ingest_outbox_context_review CHECK (
    context_review_count BETWEEN 0 AND 1
    AND (
      context_review_count = 0
      OR eligibility_decision = 'review_context'
      OR state IN ('completed', 'skipped', 'failed_terminal')
    )
  ),
  CONSTRAINT memory_ingest_outbox_state CHECK (
    state IN (
      'pending', 'claimed', 'retryable', 'completed', 'skipped',
      'expired', 'failed_terminal'
    )
  ),
  CONSTRAINT memory_ingest_outbox_decision CHECK (
    eligibility_decision IS NULL OR eligibility_decision IN (
      'send_external', 'skip_zero_call', 'route_internal',
      'block_local', 'review_context'
    )
  ),
  CONSTRAINT memory_ingest_outbox_attempts CHECK (
    max_attempts BETWEEN 1 AND 5
    AND attempt_count BETWEEN 0 AND max_attempts
  ),
  CONSTRAINT memory_ingest_outbox_lease_shape CHECK (
    (state = 'claimed'
      AND lease_token IS NOT NULL
      AND claimed_by IS NOT NULL
      AND claimed_at IS NOT NULL
      AND lease_expires_at > claimed_at)
    OR
    (state <> 'claimed'
      AND lease_token IS NULL
      AND claimed_by IS NULL
      AND claimed_at IS NULL
      AND lease_expires_at IS NULL)
  ),
  CONSTRAINT memory_ingest_outbox_terminal_shape CHECK (
    (state IN ('completed', 'skipped', 'expired', 'failed_terminal')
      AND completed_at IS NOT NULL
      AND content_sha256 IS NULL
      AND terminal_receipt_sha256 IS NOT NULL)
    OR
    (state IN ('pending', 'claimed', 'retryable')
      AND completed_at IS NULL
      AND content_sha256 IS NOT NULL
      AND terminal_receipt_sha256 IS NULL)
  ),
  CONSTRAINT memory_ingest_outbox_result_shape CHECK (
    (state = 'completed'
      AND eligibility_decision = 'send_external'
      AND successor_evidence_id IS NOT NULL
      AND successor_job_id IS NOT NULL)
    OR
    (state = 'skipped'
      AND eligibility_decision IN (
        'skip_zero_call', 'route_internal', 'block_local', 'review_context'
      )
      AND successor_evidence_id IS NULL
      AND successor_job_id IS NULL)
    OR
    (state NOT IN ('completed', 'skipped')
      AND successor_evidence_id IS NULL
      AND successor_job_id IS NULL)
  ),
  CONSTRAINT memory_ingest_outbox_error_size CHECK (
    last_error_code IS NULL
    OR (
      pg_catalog.octet_length(last_error_code) BETWEEN 1 AND 128
      AND last_error_code ~ '^[a-z][a-z0-9_]{0,127}$'
    )
  ),
  CONSTRAINT memory_ingest_outbox_error_semantics CHECK (
    (state IN ('pending', 'claimed', 'completed')
      AND last_error_code IS NULL)
    OR
    (state = 'skipped'
      AND (
        (eligibility_decision = 'review_context'
          AND last_error_code = 'context_review_unresolved')
        OR
        (eligibility_decision <> 'review_context'
          AND last_error_code IS NULL)
      ))
    OR
    (state = 'expired'
      AND last_error_code = 'content_hash_retention_expired')
    OR
    (state = 'retryable'
      AND last_error_code IN (
        'conversation_read_failed', 'lease_expired',
        'successor_write_failed', 'worker_transient_failure'
      ))
    OR
    (state = 'failed_terminal'
      AND last_error_code IN (
        'bridge_contract_violation', 'conversation_read_failed',
        'eligibility_contract_violation', 'lease_expired',
        'source_binding_mismatch', 'successor_receipt_mismatch',
        'successor_write_failed', 'worker_transient_failure'
      ))
  )
);

CREATE INDEX memory_ingest_outbox_lease_idx
  ON public.memory_ingest_outbox(
    state, available_at, source_created_at, outbox_id
  ) WHERE state IN ('pending', 'retryable', 'claimed');
CREATE INDEX memory_ingest_outbox_hash_expiry_idx
  ON public.memory_ingest_outbox(content_hash_expires_at)
  WHERE content_sha256 IS NOT NULL;

REVOKE ALL ON TABLE public.memory_ingest_outbox FROM PUBLIC;
REVOKE ALL ON TABLE public.memory_ingest_outbox FROM memory_ingest_writer;
REVOKE ALL ON TABLE public.memory_ingest_outbox FROM governed_memory_worker;
ALTER TABLE public.memory_ingest_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_ingest_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_internal ON public.memory_ingest_outbox
  TO sage USING (true) WITH CHECK (true);

CREATE FUNCTION memory_ingest_private.enqueue_memory_ingest(
  p_owner_user_id uuid,
  p_message_id uuid,
  p_thread_id uuid,
  p_content_sha256 text,
  p_source_created_at timestamptz,
  p_exchange_id uuid,
  p_window_id uuid,
  p_window_ordinal integer,
  p_window_sha256 text,
  p_policy_sha256 text
)
RETURNS TABLE(outcome text, outbox_id uuid)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  existing public.memory_ingest_outbox%ROWTYPE;
  new_outbox_id uuid;
  captured_at timestamptz;
  source_binding text;
BEGIN
  IF NOT pg_catalog.pg_has_role(
    session_user, 'memory_ingest_writer', 'MEMBER'
  ) THEN
    RAISE EXCEPTION 'memory_ingest_writer membership required'
      USING ERRCODE = '42501';
  END IF;
  actor := NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid;
  IF actor IS NULL OR p_owner_user_id IS NULL OR actor <> p_owner_user_id
     OR COALESCE(
       pg_catalog.current_setting('app.auth_context_sha256', true), ''
     ) !~ '^[0-9a-f]{64}$'
     OR p_message_id IS NULL OR p_thread_id IS NULL OR p_exchange_id IS NULL
     OR p_window_id IS NULL
     OR p_window_id = '00000000-0000-0000-0000-000000000000'::uuid
     OR p_content_sha256 IS NULL
     OR p_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_window_sha256 IS NULL
     OR p_window_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_sha256 IS NULL
     OR p_policy_sha256 !~ '^[0-9a-f]{64}$'
     OR p_source_created_at IS NULL
     OR p_window_ordinal IS NULL
     OR p_window_ordinal NOT BETWEEN 0 AND 10000 THEN
    RAISE EXCEPTION 'invalid memory ingest enqueue input'
      USING ERRCODE = '22023';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      actor::text || '|memory_ingest|' || p_message_id::text, 0
    )
  );
  SELECT value.* INTO existing
  FROM public.memory_ingest_outbox AS value
  WHERE value.owner_user_id = actor AND value.message_id = p_message_id;
  IF FOUND THEN
    source_binding := memory_ingest_private.source_binding_sha256(
      actor, p_message_id, p_thread_id, p_exchange_id, p_window_id,
      p_window_ordinal, p_window_sha256, p_content_sha256, p_policy_sha256,
      p_source_created_at
    );
    IF existing.thread_id <> p_thread_id
       OR existing.exchange_id <> p_exchange_id
       OR existing.window_id <> p_window_id
       OR existing.window_ordinal <> p_window_ordinal
       OR existing.window_sha256 <> p_window_sha256
       OR existing.source_created_at <> p_source_created_at
       OR existing.policy_sha256 <> p_policy_sha256
       OR existing.source_binding_sha256 IS DISTINCT FROM source_binding THEN
      RAISE EXCEPTION 'memory ingest enqueue replay drifted'
        USING ERRCODE = '23514';
    END IF;
    IF existing.state IN ('pending', 'claimed', 'retryable') AND (
      existing.content_sha256 IS DISTINCT FROM p_content_sha256
      OR existing.source_binding_sha256 IS DISTINCT FROM source_binding
    ) THEN
      RAISE EXCEPTION 'active memory ingest replay content hash drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT
      CASE WHEN existing.state IN (
        'completed', 'skipped', 'expired', 'failed_terminal'
      ) THEN 'terminal_replayed' ELSE 'replayed' END,
      existing.outbox_id;
    RETURN;
  END IF;
  new_outbox_id := pg_catalog.gen_random_uuid();
  captured_at := pg_catalog.transaction_timestamp();
  IF p_source_created_at < captured_at
     OR p_source_created_at > pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'message is outside the current bridge cutover transaction'
      USING ERRCODE = '22023';
  END IF;
  source_binding := memory_ingest_private.source_binding_sha256(
    actor, p_message_id, p_thread_id, p_exchange_id, p_window_id,
    p_window_ordinal, p_window_sha256, p_content_sha256, p_policy_sha256,
    p_source_created_at
  );
  INSERT INTO public.memory_ingest_outbox(
    outbox_id, owner_user_id, operation_id, message_id, thread_id,
    exchange_id, window_id, window_ordinal, window_sha256, content_sha256,
    source_binding_sha256,
    ingest_after, source_created_at, policy_sha256,
    content_hash_expires_at, purge_after, available_at, created_at, updated_at
  ) VALUES (
    new_outbox_id, actor, p_message_id, p_message_id, p_thread_id,
    p_exchange_id, p_window_id, p_window_ordinal, p_window_sha256,
    p_content_sha256, source_binding, captured_at,
    p_source_created_at, p_policy_sha256,
    captured_at + interval '24 hours', captured_at + interval '7 days',
    captured_at, captured_at, captured_at
  );
  RETURN QUERY SELECT 'enqueued'::text, new_outbox_id;
END;
$function$;

CREATE FUNCTION memory_ingest_private.lease_memory_ingest(
  p_worker_id text,
  p_limit integer,
  p_lease_seconds integer
)
RETURNS TABLE(
  outbox_id uuid,
  owner_user_id uuid,
  message_id uuid,
  thread_id uuid,
  exchange_id uuid,
  window_id uuid,
  window_ordinal integer,
  window_sha256 text,
  content_sha256 text,
  source_binding_sha256 text,
  policy_sha256 text,
  source_created_at timestamptz,
  ingest_after timestamptz,
  context_review_count integer,
  eligibility_decision text,
  lease_token uuid,
  lease_expires_at timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  candidate record;
  new_lease_token uuid;
  new_lease_expires_at timestamptz;
  captured_at timestamptz;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF pg_catalog.octet_length(COALESCE(p_worker_id, '')) NOT BETWEEN 1 AND 128
     OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
     OR p_lease_seconds IS NULL
     OR p_lease_seconds NOT BETWEEN 5 AND 300 THEN
    RAISE EXCEPTION 'invalid bridge lease input' USING ERRCODE = '22023';
  END IF;
  captured_at := pg_catalog.transaction_timestamp();
  UPDATE public.memory_ingest_outbox AS expired
  SET state = CASE
        WHEN expired.attempt_count >= expired.max_attempts THEN 'failed_terminal'
        ELSE 'retryable'
      END,
      available_at = pg_catalog.clock_timestamp(),
      lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
      lease_expires_at = NULL,
      last_error_code = 'lease_expired',
      content_sha256 = CASE WHEN expired.attempt_count >= expired.max_attempts
        THEN NULL ELSE expired.content_sha256 END,
      completed_at = CASE WHEN expired.attempt_count >= expired.max_attempts
        THEN captured_at ELSE NULL END,
      terminal_receipt_sha256 = CASE
        WHEN expired.attempt_count >= expired.max_attempts
        THEN memory_ingest_private.terminal_receipt_sha256(
          expired.source_binding_sha256, 'failed_terminal',
          expired.eligibility_decision,
          expired.context_review_count, NULL::uuid, NULL::uuid,
          'lease_expired', captured_at
        ) ELSE NULL::text END,
      updated_at = pg_catalog.clock_timestamp()
  WHERE expired.state = 'claimed'
    AND expired.lease_expires_at <= pg_catalog.clock_timestamp();

  FOR candidate IN
    SELECT value.*
    FROM public.memory_ingest_outbox AS value
    WHERE value.state IN ('pending', 'retryable')
      AND value.available_at <= pg_catalog.clock_timestamp()
      AND value.content_hash_expires_at > pg_catalog.clock_timestamp()
      AND value.attempt_count < value.max_attempts
    ORDER BY value.available_at, value.source_created_at, value.outbox_id
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  LOOP
    new_lease_token := pg_catalog.gen_random_uuid();
    new_lease_expires_at := pg_catalog.clock_timestamp()
      + pg_catalog.make_interval(secs => p_lease_seconds);
    UPDATE public.memory_ingest_outbox
    SET state = 'claimed', attempt_count = attempt_count + 1,
        lease_token = new_lease_token, claimed_by = p_worker_id,
        claimed_at = pg_catalog.clock_timestamp(),
        lease_expires_at = new_lease_expires_at,
        last_error_code = NULL, updated_at = pg_catalog.clock_timestamp()
    WHERE memory_ingest_outbox.outbox_id = candidate.outbox_id;
    RETURN QUERY SELECT candidate.outbox_id, candidate.owner_user_id,
      candidate.message_id, candidate.thread_id, candidate.exchange_id,
      candidate.window_id, candidate.window_ordinal, candidate.window_sha256,
      candidate.content_sha256, candidate.source_binding_sha256,
      candidate.policy_sha256,
      candidate.source_created_at, candidate.ingest_after,
      candidate.context_review_count,
      candidate.eligibility_decision,
      new_lease_token, new_lease_expires_at;
  END LOOP;
END;
$function$;

CREATE FUNCTION memory_ingest_private.read_memory_ingest_lease(
  p_outbox_id uuid,
  p_lease_token uuid
)
RETURNS TABLE(
  outbox_id uuid,
  owner_user_id uuid,
  message_id uuid,
  thread_id uuid,
  exchange_id uuid,
  window_id uuid,
  window_ordinal integer,
  window_sha256 text,
  content_sha256 text,
  source_binding_sha256 text,
  policy_sha256 text,
  source_created_at timestamptz,
  ingest_after timestamptz,
  context_review_count integer,
  eligibility_decision text,
  lease_token uuid,
  lease_expires_at timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_outbox_id IS NULL OR p_lease_token IS NULL THEN
    RAISE EXCEPTION 'invalid bridge lease read input' USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
  SELECT value.outbox_id, value.owner_user_id, value.message_id,
         value.thread_id, value.exchange_id, value.window_id,
         value.window_ordinal, value.window_sha256, value.content_sha256,
         value.source_binding_sha256, value.policy_sha256,
         value.source_created_at, value.ingest_after,
         value.context_review_count, value.eligibility_decision,
         value.lease_token, value.lease_expires_at
  FROM public.memory_ingest_outbox AS value
  WHERE value.outbox_id = p_outbox_id
    AND value.state = 'claimed'
    AND value.lease_token = p_lease_token
    AND value.lease_expires_at > pg_catalog.clock_timestamp();
END;
$function$;

CREATE FUNCTION memory_ingest_private.mark_memory_ingest_context_review(
  p_outbox_id uuid,
  p_lease_token uuid
)
RETURNS TABLE(outcome text, context_review_count integer)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  target public.memory_ingest_outbox%ROWTYPE;
  captured_at timestamptz;
  receipt_hash text;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_outbox_id IS NULL OR p_lease_token IS NULL THEN
    RAISE EXCEPTION 'invalid context-review mark input' USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT target
  FROM public.memory_ingest_outbox AS value
  WHERE value.outbox_id = p_outbox_id
  FOR UPDATE;
  IF target.state <> 'claimed' OR target.lease_token <> p_lease_token
     OR target.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'stale bridge lease' USING ERRCODE = '40001';
  END IF;
  captured_at := pg_catalog.transaction_timestamp();
  IF target.context_review_count = 1 THEN
    receipt_hash := memory_ingest_private.terminal_receipt_sha256(
      target.source_binding_sha256, 'skipped', 'review_context', 1,
      NULL::uuid, NULL::uuid, 'context_review_unresolved', captured_at
    );
    UPDATE public.memory_ingest_outbox
    SET state = 'skipped', eligibility_decision = 'review_context',
        content_sha256 = NULL,
        completed_at = captured_at,
        terminal_receipt_sha256 = receipt_hash,
        lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
        lease_expires_at = NULL,
        last_error_code = 'context_review_unresolved',
        updated_at = captured_at
    WHERE memory_ingest_outbox.outbox_id = target.outbox_id
      AND memory_ingest_outbox.state = 'claimed'
      AND memory_ingest_outbox.lease_token = p_lease_token
      AND memory_ingest_outbox.context_review_count = 1;
    RETURN QUERY SELECT 'terminal_unresolved'::text, 1;
    RETURN;
  ELSIF target.context_review_count <> 0 THEN
    RAISE EXCEPTION 'invalid context review count' USING ERRCODE = '23514';
  END IF;
  UPDATE public.memory_ingest_outbox
  SET context_review_count = 1, eligibility_decision = 'review_context',
      updated_at = captured_at
  WHERE memory_ingest_outbox.outbox_id = target.outbox_id
    AND memory_ingest_outbox.state = 'claimed'
    AND memory_ingest_outbox.lease_token = p_lease_token
    AND memory_ingest_outbox.context_review_count = 0;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'context review CAS failed' USING ERRCODE = '40001';
  END IF;
  RETURN QUERY SELECT 'marked'::text, 1;
END;
$function$;

CREATE FUNCTION memory_ingest_private.ack_memory_ingest(
  p_outbox_id uuid,
  p_lease_token uuid,
  p_decision text,
  p_successor_evidence_id uuid,
  p_successor_job_id uuid
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  target public.memory_ingest_outbox%ROWTYPE;
  captured_at timestamptz;
  receipt_hash text;
  resulting_state text;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_outbox_id IS NULL OR p_lease_token IS NULL OR p_decision IS NULL
     OR p_decision = 'review_context'
     OR p_decision NOT IN (
       'send_external', 'skip_zero_call', 'route_internal', 'block_local'
     ) OR (
       p_decision = 'send_external'
       AND (p_successor_evidence_id IS NULL OR p_successor_job_id IS NULL)
     ) OR (
       p_decision <> 'send_external'
       AND (p_successor_evidence_id IS NOT NULL OR p_successor_job_id IS NOT NULL)
     ) THEN
    RAISE EXCEPTION 'invalid bridge acknowledgment' USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT target
  FROM public.memory_ingest_outbox AS value
  WHERE value.outbox_id = p_outbox_id
  FOR UPDATE;
  resulting_state := CASE WHEN p_decision = 'send_external'
    THEN 'completed' ELSE 'skipped' END;
  IF target.state IN ('completed', 'skipped') THEN
    receipt_hash := memory_ingest_private.terminal_receipt_sha256(
      target.source_binding_sha256, target.state,
      target.eligibility_decision, target.context_review_count,
      target.successor_evidence_id, target.successor_job_id,
      target.last_error_code, target.completed_at
    );
    IF target.state <> resulting_state
       OR target.eligibility_decision <> p_decision
       OR target.successor_evidence_id
            IS DISTINCT FROM p_successor_evidence_id
       OR target.successor_job_id IS DISTINCT FROM p_successor_job_id
       OR target.last_error_code IS NOT NULL
       OR target.terminal_receipt_sha256 <> receipt_hash THEN
      RAISE EXCEPTION 'bridge acknowledgment replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN 'replayed';
  END IF;
  IF target.state <> 'claimed' OR target.lease_token <> p_lease_token
     OR target.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'stale bridge lease' USING ERRCODE = '40001';
  END IF;
  captured_at := pg_catalog.transaction_timestamp();
  receipt_hash := memory_ingest_private.terminal_receipt_sha256(
    target.source_binding_sha256, resulting_state, p_decision,
    target.context_review_count, p_successor_evidence_id,
    p_successor_job_id, NULL::text, captured_at
  );
  UPDATE public.memory_ingest_outbox
  SET state = resulting_state,
      eligibility_decision = p_decision,
      successor_evidence_id = p_successor_evidence_id,
      successor_job_id = p_successor_job_id,
      content_sha256 = NULL, completed_at = captured_at,
      terminal_receipt_sha256 = receipt_hash,
      lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
      lease_expires_at = NULL, updated_at = captured_at
  WHERE memory_ingest_outbox.outbox_id = target.outbox_id;
  RETURN resulting_state;
END;
$function$;

CREATE FUNCTION memory_ingest_private.fail_memory_ingest(
  p_outbox_id uuid,
  p_lease_token uuid,
  p_failure_mode text,
  p_error_code text,
  p_retry_after_seconds integer
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  target public.memory_ingest_outbox%ROWTYPE;
  resulting_state text;
  captured_at timestamptz;
  receipt_hash text;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_outbox_id IS NULL OR p_lease_token IS NULL
     OR p_failure_mode IS NULL
     OR p_failure_mode NOT IN ('retryable', 'failed_terminal')
     OR p_error_code IS NULL
     OR pg_catalog.octet_length(COALESCE(p_error_code, '')) NOT BETWEEN 1 AND 128
     OR p_retry_after_seconds IS NULL
     OR p_retry_after_seconds NOT BETWEEN 0 AND 3600
     OR (p_failure_mode = 'failed_terminal' AND p_retry_after_seconds <> 0)
     OR (
       p_failure_mode = 'retryable'
       AND p_error_code NOT IN (
         'conversation_read_failed', 'successor_write_failed',
         'worker_transient_failure'
       )
     )
     OR (
       p_failure_mode = 'failed_terminal'
       AND p_error_code NOT IN (
         'bridge_contract_violation', 'eligibility_contract_violation',
         'source_binding_mismatch', 'successor_receipt_mismatch'
       )
     ) THEN
    RAISE EXCEPTION 'invalid bridge failure input' USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT target
  FROM public.memory_ingest_outbox AS value
  WHERE value.outbox_id = p_outbox_id
  FOR UPDATE;
  IF target.state = 'failed_terminal' THEN
    receipt_hash := memory_ingest_private.terminal_receipt_sha256(
      target.source_binding_sha256, target.state,
      target.eligibility_decision, target.context_review_count,
      NULL::uuid, NULL::uuid, target.last_error_code, target.completed_at
    );
    IF NOT (
         (
           p_failure_mode = 'failed_terminal'
           AND p_error_code IN (
             'bridge_contract_violation', 'eligibility_contract_violation',
             'source_binding_mismatch', 'successor_receipt_mismatch'
           )
         )
         OR
         (
           p_failure_mode = 'retryable'
           AND target.attempt_count >= target.max_attempts
           AND p_retry_after_seconds = 0
           AND p_error_code IN (
             'conversation_read_failed', 'successor_write_failed',
             'worker_transient_failure'
           )
         )
       )
       OR target.last_error_code <> p_error_code
       OR target.terminal_receipt_sha256 <> receipt_hash THEN
      RAISE EXCEPTION 'bridge failure replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN 'replayed';
  END IF;
  IF target.state <> 'claimed' OR target.lease_token <> p_lease_token
     OR target.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'stale bridge lease' USING ERRCODE = '40001';
  END IF;
  IF p_failure_mode = 'retryable'
     AND target.attempt_count >= target.max_attempts
     AND p_retry_after_seconds <> 0 THEN
    RAISE EXCEPTION 'exhausted retry must use zero retry delay'
      USING ERRCODE = '22023';
  END IF;
  resulting_state := CASE
    WHEN p_failure_mode = 'retryable'
         AND target.attempt_count < target.max_attempts THEN 'retryable'
    ELSE 'failed_terminal' END;
  captured_at := pg_catalog.transaction_timestamp();
  IF resulting_state = 'failed_terminal' THEN
    receipt_hash := memory_ingest_private.terminal_receipt_sha256(
      target.source_binding_sha256, 'failed_terminal',
      target.eligibility_decision, target.context_review_count,
      NULL::uuid, NULL::uuid, p_error_code, captured_at
    );
  END IF;
  UPDATE public.memory_ingest_outbox
  SET state = resulting_state,
      available_at = CASE WHEN resulting_state = 'retryable'
        THEN pg_catalog.clock_timestamp()
          + pg_catalog.make_interval(secs => p_retry_after_seconds)
        ELSE available_at END,
      content_sha256 = CASE WHEN resulting_state = 'failed_terminal'
        THEN NULL ELSE content_sha256 END,
      completed_at = CASE WHEN resulting_state = 'failed_terminal'
        THEN captured_at ELSE NULL END,
      terminal_receipt_sha256 = receipt_hash,
      lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
      lease_expires_at = NULL, last_error_code = p_error_code,
      updated_at = captured_at
  WHERE memory_ingest_outbox.outbox_id = target.outbox_id;
  RETURN resulting_state;
END;
$function$;

CREATE FUNCTION memory_ingest_private.expire_memory_ingest(p_limit integer)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  expired_count integer;
  captured_at timestamptz;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION 'invalid bridge expiry limit' USING ERRCODE = '22023';
  END IF;
  captured_at := pg_catalog.transaction_timestamp();
  WITH candidates AS (
    SELECT value.outbox_id
    FROM public.memory_ingest_outbox AS value
    WHERE value.state IN ('pending', 'retryable')
      AND value.content_hash_expires_at <= pg_catalog.clock_timestamp()
    ORDER BY value.content_hash_expires_at, value.outbox_id
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  ), expired AS (
    UPDATE public.memory_ingest_outbox AS value
    SET state = 'expired', content_sha256 = NULL,
        completed_at = captured_at,
        terminal_receipt_sha256 =
          memory_ingest_private.terminal_receipt_sha256(
            value.source_binding_sha256, 'expired',
            value.eligibility_decision, value.context_review_count,
            NULL::uuid, NULL::uuid,
            'content_hash_retention_expired', captured_at
          ),
        last_error_code = 'content_hash_retention_expired',
        updated_at = pg_catalog.clock_timestamp()
    FROM candidates
    WHERE value.outbox_id = candidates.outbox_id
    RETURNING 1
  )
  SELECT pg_catalog.count(*)::integer INTO expired_count FROM expired;
  RETURN expired_count;
END;
$function$;

CREATE FUNCTION memory_ingest_private.purge_terminal_memory_ingest(
  p_limit integer
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  purged_count integer;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION 'invalid bridge purge limit' USING ERRCODE = '22023';
  END IF;
  WITH candidates AS (
    SELECT value.outbox_id
    FROM public.memory_ingest_outbox AS value
    WHERE value.state IN (
      'completed', 'skipped', 'expired', 'failed_terminal'
    )
      AND value.purge_after <= pg_catalog.clock_timestamp()
    ORDER BY value.purge_after, value.outbox_id
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  ), purged AS (
    DELETE FROM public.memory_ingest_outbox AS value
    USING candidates
    WHERE value.outbox_id = candidates.outbox_id
    RETURNING 1
  )
  SELECT pg_catalog.count(*)::integer INTO purged_count FROM purged;
  RETURN purged_count;
END;
$function$;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA memory_ingest_private
  FROM PUBLIC, memory_ingest_writer, governed_memory_worker;
GRANT EXECUTE ON FUNCTION memory_ingest_private.enqueue_memory_ingest(
  uuid,uuid,uuid,text,timestamptz,uuid,uuid,integer,text,text
) TO memory_ingest_writer;
GRANT EXECUTE ON FUNCTION
  memory_ingest_private.lease_memory_ingest(text,integer,integer),
  memory_ingest_private.read_memory_ingest_lease(uuid,uuid),
  memory_ingest_private.mark_memory_ingest_context_review(uuid,uuid),
  memory_ingest_private.ack_memory_ingest(uuid,uuid,text,uuid,uuid),
  memory_ingest_private.fail_memory_ingest(uuid,uuid,text,text,integer),
  memory_ingest_private.expire_memory_ingest(integer),
  memory_ingest_private.purge_terminal_memory_ingest(integer)
TO governed_memory_worker;

DO $postflight$
DECLARE
  forbidden_role text;
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
      AND relation.relname = 'memory_ingest_outbox'
      AND relation.relrowsecurity
      AND relation.relforcerowsecurity
      AND relation.relowner = 'sage'::regrole
  ) THEN
    RAISE EXCEPTION 'bridge table owner or forced RLS differs';
  END IF;
  IF pg_catalog.has_table_privilege(
    'memory_ingest_writer', 'public.memory_ingest_outbox', 'SELECT'
  ) OR pg_catalog.has_table_privilege(
    'memory_ingest_writer', 'public.memory_ingest_outbox', 'INSERT'
  ) OR pg_catalog.has_table_privilege(
    'memory_ingest_writer', 'public.memory_ingest_outbox', 'UPDATE'
  ) OR pg_catalog.has_table_privilege(
    'memory_ingest_writer', 'public.memory_ingest_outbox', 'DELETE'
  ) OR pg_catalog.has_table_privilege(
    'governed_memory_worker', 'public.memory_ingest_outbox', 'SELECT'
  ) OR pg_catalog.has_table_privilege(
    'governed_memory_worker', 'public.memory_ingest_outbox', 'UPDATE'
  ) OR pg_catalog.has_table_privilege(
    'governed_memory_worker', 'public.memory_ingest_outbox', 'INSERT'
  ) OR pg_catalog.has_table_privilege(
    'governed_memory_worker', 'public.memory_ingest_outbox', 'DELETE'
  ) THEN
    RAISE EXCEPTION 'bridge runtime role has direct table authority';
  END IF;
  FOREACH forbidden_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF pg_catalog.to_regrole(forbidden_role) IS NOT NULL AND (
      pg_catalog.has_table_privilege(
        forbidden_role, 'public.memory_ingest_outbox', 'SELECT'
      ) OR pg_catalog.has_table_privilege(
        forbidden_role, 'public.memory_ingest_outbox', 'INSERT'
      ) OR pg_catalog.has_table_privilege(
        forbidden_role, 'public.memory_ingest_outbox', 'UPDATE'
      ) OR pg_catalog.has_table_privilege(
        forbidden_role, 'public.memory_ingest_outbox', 'DELETE'
      )
    ) THEN
      RAISE EXCEPTION 'forbidden role % can access bridge table', forbidden_role;
    END IF;
  END LOOP;
END;
$postflight$;
