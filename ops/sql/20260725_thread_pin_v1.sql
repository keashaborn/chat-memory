BEGIN;

ALTER TABLE public.threads
    ADD COLUMN IF NOT EXISTS pinned_at timestamp with time zone;

COMMENT ON COLUMN public.threads.pinned_at IS
    'Owner-selected sidebar pin time; NULL means not pinned';

CREATE INDEX IF NOT EXISTS threads_owner_visible_pin_order_idx
    ON public.threads (
        owner_user_id,
        pinned_at DESC NULLS LAST,
        updated_at DESC
    )
    WHERE archived = false
      AND owner_user_id IS NOT NULL;

COMMIT;
