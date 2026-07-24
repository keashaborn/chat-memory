BEGIN;

ALTER TABLE public.threads
    DROP CONSTRAINT IF EXISTS threads_title_source_check;

ALTER TABLE public.threads
    DROP COLUMN IF EXISTS title_source;

COMMIT;
