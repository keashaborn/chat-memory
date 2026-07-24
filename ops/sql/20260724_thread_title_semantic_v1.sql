BEGIN;

ALTER TABLE public.threads
    DROP CONSTRAINT IF EXISTS threads_title_source_check;

-- A never-titled new thread remains eligible for its first semantic title.
-- Existing generated or manually selected titles remain frozen.
UPDATE public.threads
SET title_source = 'placeholder'
WHERE title_source = 'automatic'
  AND lower(btrim(title)) IN ('new chat', 'new topic');

ALTER TABLE public.threads
    ALTER COLUMN title_source SET DEFAULT 'placeholder';

ALTER TABLE public.threads
    ADD CONSTRAINT threads_title_source_check
    CHECK (title_source IN ('placeholder', 'automatic', 'manual'));

COMMENT ON COLUMN public.threads.title_source IS
    'placeholder may receive one backend-generated title; automatic and manual titles are frozen';

COMMIT;
