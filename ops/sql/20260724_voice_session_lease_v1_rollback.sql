BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'voice session lease rollback must run as sage, current_user=%',
      current_user;
  END IF;
END
$$;

DROP TABLE IF EXISTS public.voice_session_lease;

COMMIT;
