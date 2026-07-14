\set ON_ERROR_STOP on
BEGIN;
SET LOCAL ROLE brains_app;

DO $block$
DECLARE
  owner_a constant uuid := '557ea042-cb82-48f8-9429-472e96c957ef';
  owner_b constant uuid := '1240822d-ac9a-4096-95aa-e2b24d36ef50';
  thread_a constant uuid := 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1';
  thread_b constant uuid := 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1';
  failed boolean;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name='chat_log'
      AND column_name='owner_user_id' AND data_type='uuid'
  ) THEN
    RAISE EXCEPTION 'chat_log.owner_user_id uuid is missing';
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.chat_log
    WHERE owner_user_id IS NOT NULL
      AND user_id IS DISTINCT FROM owner_user_id::text
  ) THEN
    RAISE EXCEPTION 'chat_log has owner/user mismatch';
  END IF;

  PERFORM set_config('app.user_id', owner_a::text, true);
  INSERT INTO public.threads(id, owner_user_id, user_id, title)
  VALUES (thread_a, owner_a, owner_a::text, 'owner test A');

  PERFORM set_config('app.user_id', owner_b::text, true);
  INSERT INTO public.threads(id, owner_user_id, user_id, title)
  VALUES (thread_b, owner_b, owner_b::text, 'owner test B');

  PERFORM set_config('app.user_id', owner_a::text, true);

  INSERT INTO public.chat_log(
    id, owner_user_id, user_id, source, text, thread_id, created_at
  ) VALUES (
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2',
    owner_a,
    owner_a::text,
    'owner-test',
    'owned row',
    thread_a,
    now()
  );

  failed := false;
  BEGIN
    INSERT INTO public.chat_log(
      id, owner_user_id, user_id, source, text, created_at
    ) VALUES (
      'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3',
      NULL,
      owner_a::text,
      'owner-test',
      'missing owner',
      now()
    );
  EXCEPTION WHEN not_null_violation THEN
    failed := true;
  END;
  IF NOT failed THEN
    RAISE EXCEPTION 'missing owner insert was accepted';
  END IF;

  failed := false;
  BEGIN
    INSERT INTO public.chat_log(
      id, owner_user_id, user_id, source, text, created_at
    ) VALUES (
      'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4',
      owner_a,
      owner_b::text,
      'owner-test',
      'mismatched owner',
      now()
    );
  EXCEPTION WHEN check_violation THEN
    failed := true;
  END;
  IF NOT failed THEN
    RAISE EXCEPTION 'owner mismatch insert was accepted';
  END IF;

  failed := false;
  BEGIN
    INSERT INTO public.chat_log(
      id, owner_user_id, user_id, source, text, created_at
    ) VALUES (
      'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa6',
      owner_b,
      owner_b::text,
      'owner-test',
      'foreign owner under actor A',
      now()
    );
  EXCEPTION WHEN insufficient_privilege THEN
    failed := true;
  END;
  IF NOT failed THEN
    RAISE EXCEPTION 'RLS accepted a foreign-owner insert';
  END IF;

  failed := false;
  BEGIN
    INSERT INTO public.chat_log(
      id, owner_user_id, user_id, source, text, thread_id, created_at
    ) VALUES (
      'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa5',
      owner_a,
      owner_a::text,
      'owner-test',
      'cross-owner thread',
      thread_b,
      now()
    );
  EXCEPTION WHEN foreign_key_violation THEN
    failed := true;
  END;
  IF NOT failed THEN
    RAISE EXCEPTION 'cross-owner thread attachment was accepted';
  END IF;

  failed := false;
  BEGIN
    UPDATE public.chat_log
    SET owner_user_id=owner_b, user_id=owner_b::text
    WHERE id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2';
  EXCEPTION WHEN check_violation THEN
    failed := true;
  END;
  IF NOT failed THEN
    RAISE EXCEPTION 'owner mutation was accepted';
  END IF;

  PERFORM set_config('app.user_id', owner_b::text, true);
  IF EXISTS (
    SELECT 1 FROM public.chat_log
    WHERE id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2'
  ) THEN
    RAISE EXCEPTION 'cross-owner chat_log row was visible through RLS';
  END IF;
  IF EXISTS (
    SELECT 1 FROM public.threads WHERE id=thread_a
  ) THEN
    RAISE EXCEPTION 'cross-owner thread row was visible through RLS';
  END IF;

  PERFORM set_config('app.user_id', '', true);
  IF EXISTS (SELECT 1 FROM public.chat_log LIMIT 1)
     OR EXISTS (SELECT 1 FROM public.threads LIMIT 1) THEN
    RAISE EXCEPTION 'raw rows were visible without actor context';
  END IF;
END
$block$;

RESET ROLE;
ROLLBACK;
