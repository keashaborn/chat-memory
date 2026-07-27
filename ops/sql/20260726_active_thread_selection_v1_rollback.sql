BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'active thread selection rollback must run as sage, current_user=%',
      current_user;
  END IF;
END
$block$;

DROP TABLE IF EXISTS public.active_thread_selection;

COMMIT;
