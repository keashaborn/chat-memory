BEGIN;

ALTER TABLE public.threads
    ADD COLUMN IF NOT EXISTS title_source text;

ALTER TABLE public.threads
    ALTER COLUMN title_source SET DEFAULT 'automatic';

-- Preserve every existing title. Some may already have been chosen manually,
-- and their provenance cannot be reconstructed safely.
UPDATE public.threads
SET title_source = 'manual'
WHERE title_source IS NULL;

ALTER TABLE public.threads
    ALTER COLUMN title_source SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.threads'::regclass
          AND conname = 'threads_title_source_check'
    ) THEN
        ALTER TABLE public.threads
            ADD CONSTRAINT threads_title_source_check
            CHECK (title_source IN ('automatic', 'manual'));
    END IF;
END
$$;

COMMENT ON COLUMN public.threads.title_source IS
    'automatic titles may evolve; manual titles are protected from automatic replacement';

COMMIT;
