BEGIN;

DO $test$
DECLARE
  synthetic_chat_id constant uuid :=
    'f5555555-5555-4555-8555-555555555555'::uuid;
  synthetic_owner constant uuid :=
    '1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid;
  chat_rows_before bigint;
  jobs_before bigint;
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid='public.chat_log'::regclass
      AND tgname='chat_log_enqueue_memory_v1_consolidation'
      AND tgenabled='D'
      AND NOT tgisinternal
  ) THEN
    RAISE EXCEPTION 'V4 trigger is not disabled during freeze test';
  END IF;
  IF EXISTS (
    SELECT 1 FROM public.chat_log WHERE id=synthetic_chat_id
  ) OR EXISTS (
    SELECT 1
    FROM memory.consolidation_job
    WHERE source_system='public.chat_log'
      AND source_external_id=synthetic_chat_id::text
  ) THEN
    RAISE EXCEPTION 'synthetic freeze-test identifiers already exist';
  END IF;

  SELECT count(*) INTO chat_rows_before FROM public.chat_log;
  SELECT count(*) INTO jobs_before FROM memory.consolidation_job;

  INSERT INTO public.chat_log(
    id,user_id,source,text,owner_user_id
  ) VALUES (
    synthetic_chat_id,
    synthetic_owner::text,
    'frontend/chat:user',
    'Synthetic rollback-only Phase 0 V4 freeze test.',
    synthetic_owner
  );

  IF (SELECT count(*) FROM public.chat_log) <> chat_rows_before + 1 THEN
    RAISE EXCEPTION 'chat logging failed while V4 trigger was disabled';
  END IF;
  IF (SELECT count(*) FROM memory.consolidation_job) <> jobs_before THEN
    RAISE EXCEPTION 'disabled V4 trigger created a consolidation job';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.consolidation_job
    WHERE source_system='public.chat_log'
      AND source_external_id=synthetic_chat_id::text
  ) THEN
    RAISE EXCEPTION 'disabled V4 trigger created the synthetic job';
  END IF;
END
$test$;

ROLLBACK;

SELECT (
  NOT EXISTS (
    SELECT 1
    FROM public.chat_log
    WHERE id='f5555555-5555-4555-8555-555555555555'::uuid
  )
  AND NOT EXISTS (
    SELECT 1
    FROM memory.consolidation_job
    WHERE source_system='public.chat_log'
      AND source_external_id='f5555555-5555-4555-8555-555555555555'
  )
)::integer AS memory_v1_freeze_v4_capture_zero_write;
