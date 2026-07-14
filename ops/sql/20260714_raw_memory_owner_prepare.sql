BEGIN;

ALTER TABLE public.chat_log
  ADD COLUMN IF NOT EXISTS owner_user_id uuid;

ALTER TABLE public.threads
  ADD COLUMN IF NOT EXISTS owner_user_id uuid;

-- Only UUID-shaped legacy identifiers are self-authenticating enough to
-- backfill. Named aliases (guest/debug/anon/test) remain quarantined with a
-- NULL owner and cannot enter authenticated retrieval or consolidation.
UPDATE public.chat_log
SET owner_user_id = user_id::uuid
WHERE owner_user_id IS NULL
  AND user_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';

UPDATE public.threads
SET owner_user_id = user_id::uuid
WHERE owner_user_id IS NULL
  AND user_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';

CREATE INDEX IF NOT EXISTS chat_log_owner_thread_time_idx
  ON public.chat_log(owner_user_id, thread_id, created_at DESC)
  WHERE owner_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS threads_owner_updated_idx
  ON public.threads(owner_user_id, updated_at DESC)
  WHERE owner_user_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS threads_owner_id_uq
  ON public.threads(owner_user_id, id);

DO $block$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'public.chat_log'::regclass
      AND conname = 'chat_log_owner_matches_legacy_ck'
  ) THEN
    ALTER TABLE public.chat_log
      ADD CONSTRAINT chat_log_owner_matches_legacy_ck
      CHECK (owner_user_id IS NULL OR user_id = owner_user_id::text)
      NOT VALID;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'public.threads'::regclass
      AND conname = 'threads_owner_matches_legacy_ck'
  ) THEN
    ALTER TABLE public.threads
      ADD CONSTRAINT threads_owner_matches_legacy_ck
      CHECK (owner_user_id IS NULL OR user_id = owner_user_id::text)
      NOT VALID;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'public.chat_log'::regclass
      AND conname = 'chat_log_owner_thread_fk'
  ) THEN
    ALTER TABLE public.chat_log
      ADD CONSTRAINT chat_log_owner_thread_fk
      FOREIGN KEY (owner_user_id, thread_id)
      REFERENCES public.threads(owner_user_id, id)
      NOT VALID;
  END IF;
END
$block$;

COMMENT ON COLUMN public.chat_log.owner_user_id IS
  'Canonical authenticated Supabase UUID. NULL denotes quarantined legacy data.';
COMMENT ON COLUMN public.threads.owner_user_id IS
  'Canonical authenticated Supabase UUID. NULL denotes quarantined legacy data.';

COMMIT;
