BEGIN;

DROP TABLE IF EXISTS public.chat_attachments;
DROP INDEX IF EXISTS public.chat_log_id_owner_thread_chat_attachments_uq;
DROP INDEX IF EXISTS public.threads_id_owner_user_id_chat_attachments_uq;

COMMIT;
