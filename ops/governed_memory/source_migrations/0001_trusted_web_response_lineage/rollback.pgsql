\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE trusted_web.response_transcript_v1
  DROP CONSTRAINT response_transcript_v1_user_chat_log_id_fkey,
  DROP CONSTRAINT response_transcript_v1_assistant_chat_log_id_fkey;

ALTER TABLE trusted_web.response_transcript_v1
  ADD CONSTRAINT response_transcript_v1_user_chat_log_id_fkey
    FOREIGN KEY (user_chat_log_id)
    REFERENCES public.chat_log(id)
    ON DELETE CASCADE NOT VALID,
  ADD CONSTRAINT response_transcript_v1_assistant_chat_log_id_fkey
    FOREIGN KEY (assistant_chat_log_id)
    REFERENCES public.chat_log(id)
    ON DELETE CASCADE NOT VALID;

ALTER TABLE trusted_web.response_transcript_v1
  VALIDATE CONSTRAINT response_transcript_v1_user_chat_log_id_fkey;
ALTER TABLE trusted_web.response_transcript_v1
  VALIDATE CONSTRAINT response_transcript_v1_assistant_chat_log_id_fkey;

COMMIT;
