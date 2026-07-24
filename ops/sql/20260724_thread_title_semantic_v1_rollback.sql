BEGIN;

ALTER TABLE public.threads
    DROP CONSTRAINT IF EXISTS threads_title_source_check;

UPDATE public.threads
SET title_source = 'automatic'
WHERE title_source = 'placeholder';

ALTER TABLE public.threads
    ALTER COLUMN title_source SET DEFAULT 'automatic';

ALTER TABLE public.threads
    ADD CONSTRAINT threads_title_source_check
    CHECK (title_source IN ('automatic', 'manual'));

COMMENT ON COLUMN public.threads.title_source IS
    'automatic titles may evolve; manual titles are protected from automatic replacement';

COMMIT;
