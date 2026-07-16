BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 V4 capture freeze rollback must run as sage, current_user=%',
      current_user;
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid='public.chat_log'::regclass
      AND tgname='chat_log_enqueue_memory_v1_consolidation'
      AND NOT tgisinternal
  ) THEN
    RAISE EXCEPTION 'V4 chat-log consolidation trigger is missing';
  END IF;
END
$block$;

ALTER TABLE public.chat_log
  ENABLE TRIGGER chat_log_enqueue_memory_v1_consolidation;

DO $block$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid='public.chat_log'::regclass
      AND tgname='chat_log_enqueue_memory_v1_consolidation'
      AND tgenabled='O'
      AND NOT tgisinternal
  ) THEN
    RAISE EXCEPTION 'V4 chat-log consolidation trigger was not re-enabled';
  END IF;
END
$block$;

COMMIT;
