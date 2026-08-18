BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'conversation Zep sync migration must run as sage, current_user=%',
      current_user;
  END IF;
END
$$;

SELECT pg_catalog.pg_advisory_xact_lock(
  pg_catalog.hashtextextended('conversation_zep_sync_outbox_v1_install', 0)
);

CREATE SCHEMA IF NOT EXISTS conversation_sync_private AUTHORIZATION sage;
REVOKE ALL ON SCHEMA conversation_sync_private FROM PUBLIC;
GRANT USAGE ON SCHEMA conversation_sync_private TO brains_app;

CREATE TABLE conversation_sync_private.zep_turn_outbox (
  job_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  user_message_id uuid NOT NULL
    REFERENCES public.chat_log(id) ON DELETE CASCADE,
  assistant_message_id uuid NOT NULL
    REFERENCES public.chat_log(id) ON DELETE CASCADE,
  state text NOT NULL DEFAULT 'pending'
    CHECK (state IN (
      'pending','processing','retry','completed','failed_terminal'
    )),
  attempt_count integer NOT NULL DEFAULT 0
    CHECK (attempt_count >= 0),
  available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  lease_token uuid,
  lease_expires_at timestamptz,
  last_error_code text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  completed_at timestamptz,
  CONSTRAINT zep_turn_outbox_job_is_assistant
    CHECK (job_id=assistant_message_id),
  CONSTRAINT zep_turn_outbox_distinct_messages
    CHECK (user_message_id<>assistant_message_id),
  CONSTRAINT zep_turn_outbox_lease_shape
    CHECK (
      (state='processing' AND lease_token IS NOT NULL
        AND lease_expires_at IS NOT NULL)
      OR
      (state<>'processing' AND lease_token IS NULL
        AND lease_expires_at IS NULL)
    ),
  CONSTRAINT zep_turn_outbox_completion_shape
    CHECK (
      (state IN ('completed','failed_terminal') AND completed_at IS NOT NULL)
      OR
      (state NOT IN ('completed','failed_terminal') AND completed_at IS NULL)
    ),
  CONSTRAINT zep_turn_outbox_error_shape
    CHECK (last_error_code IS NULL OR (
      length(last_error_code) BETWEEN 1 AND 120
      AND last_error_code ~ '^[A-Za-z0-9_.:-]+$'
    )),
  UNIQUE(owner_user_id,thread_id,user_message_id,assistant_message_id)
);

CREATE INDEX zep_turn_outbox_claim_idx
  ON conversation_sync_private.zep_turn_outbox(
    state,available_at,created_at,job_id
  )
  WHERE state IN ('pending','processing','retry');

CREATE INDEX zep_turn_outbox_owner_thread_idx
  ON conversation_sync_private.zep_turn_outbox(
    owner_user_id,thread_id,created_at,job_id
  );

REVOKE ALL ON conversation_sync_private.zep_turn_outbox FROM PUBLIC;
REVOKE ALL ON conversation_sync_private.zep_turn_outbox FROM brains_app;
GRANT SELECT ON conversation_sync_private.zep_turn_outbox TO brains_app;
GRANT INSERT (
  job_id,owner_user_id,thread_id,user_message_id,assistant_message_id
) ON conversation_sync_private.zep_turn_outbox TO brains_app;
GRANT UPDATE (
  state,attempt_count,available_at,lease_token,lease_expires_at,
  last_error_code,updated_at,completed_at
) ON conversation_sync_private.zep_turn_outbox TO brains_app;

COMMENT ON TABLE conversation_sync_private.zep_turn_outbox IS
  'Content-free restart-safe synchronization ledger. Message bodies remain canonical in public.chat_log and are read only while a leased job is processed. A terminal failure blocks later turns in the same thread until operator repair.';

COMMIT;
