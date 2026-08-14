\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';

DO $preflight$
DECLARE
  relation_name text := 'trusted_web.response_transcript_v1';
BEGIN
  IF current_database() <> 'memory' OR current_user <> 'sage' THEN
    RAISE EXCEPTION 'trusted web lineage migration identity mismatch';
  END IF;
  IF to_regclass(relation_name) IS NULL
     OR to_regclass('public.chat_log') IS NULL
     OR to_regclass('public.chat_log_id_owner_thread_chat_attachments_uq') IS NULL THEN
    RAISE EXCEPTION 'trusted web lineage migration prerequisite absent';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM trusted_web.response_transcript_v1 AS transcript
    JOIN public.chat_log AS message
      ON message.id IN (
        transcript.user_chat_log_id,
        transcript.assistant_chat_log_id
      )
    WHERE (transcript.owner_user_id, transcript.thread_id)
          IS DISTINCT FROM (message.owner_user_id, message.thread_id)
  ) THEN
    RAISE EXCEPTION 'trusted web transcript lineage mismatch';
  END IF;
END;
$preflight$;

ALTER TABLE trusted_web.response_transcript_v1
  DROP CONSTRAINT response_transcript_v1_user_chat_log_id_fkey,
  DROP CONSTRAINT response_transcript_v1_assistant_chat_log_id_fkey;

ALTER TABLE trusted_web.response_transcript_v1
  ADD CONSTRAINT response_transcript_v1_user_chat_log_id_fkey
    FOREIGN KEY (user_chat_log_id, owner_user_id, thread_id)
    REFERENCES public.chat_log(id, owner_user_id, thread_id)
    ON DELETE CASCADE NOT VALID,
  ADD CONSTRAINT response_transcript_v1_assistant_chat_log_id_fkey
    FOREIGN KEY (assistant_chat_log_id, owner_user_id, thread_id)
    REFERENCES public.chat_log(id, owner_user_id, thread_id)
    ON DELETE CASCADE NOT VALID;

ALTER TABLE trusted_web.response_transcript_v1
  VALIDATE CONSTRAINT response_transcript_v1_user_chat_log_id_fkey;
ALTER TABLE trusted_web.response_transcript_v1
  VALIDATE CONSTRAINT response_transcript_v1_assistant_chat_log_id_fkey;

COMMIT;
