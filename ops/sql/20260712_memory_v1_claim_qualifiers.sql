BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 qualifiers migration must run as sage, current_user=%', current_user;
  END IF;
END
$$;

ALTER TABLE memory.claim
  ADD COLUMN IF NOT EXISTS qualifiers jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMIT;
