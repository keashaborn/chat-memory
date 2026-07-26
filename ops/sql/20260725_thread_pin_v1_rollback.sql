BEGIN;

DROP INDEX IF EXISTS public.threads_owner_visible_pin_order_idx;

ALTER TABLE public.threads
    DROP COLUMN IF EXISTS pinned_at;

COMMIT;
