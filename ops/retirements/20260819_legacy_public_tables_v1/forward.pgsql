BEGIN;

DO $retirement$
DECLARE
  expected_database text;
  backup_sha256 text;
  restore_receipt_sha256 text;
  external_function_refs integer;
  external_view_refs integer;
  external_foreign_keys integer;
  target_triggers integer;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'legacy public-table retirement must run as sage, current_user=%',
      current_user;
  END IF;

  expected_database := COALESCE(
    NULLIF(current_setting(
      'lifeswitch.legacy_public_tables_target_database', true
    ), ''),
    'memory'
  );
  IF current_database() <> expected_database THEN
    RAISE EXCEPTION
      'legacy public-table target mismatch: expected %, current %',
      expected_database, current_database();
  END IF;

  backup_sha256 := COALESCE(current_setting(
    'lifeswitch.legacy_public_tables_backup_sha256', true
  ), '');
  restore_receipt_sha256 := COALESCE(current_setting(
    'lifeswitch.legacy_public_tables_restore_receipt_sha256', true
  ), '');
  IF backup_sha256 !~ '^[0-9a-f]{64}$'
     OR restore_receipt_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION
      'hash-bound public-table backup and restore receipt are required';
  END IF;

  IF to_regclass('public.chat_messages') IS NULL
     OR to_regclass('public.chat_sessions') IS NULL
     OR to_regclass('public.feedback_signals') IS NULL
     OR to_regclass('public.vantage_answer_trace') IS NULL
     OR to_regclass('public.vs_profiles') IS NULL THEN
    RAISE EXCEPTION 'legacy public-table target set is incomplete';
  END IF;

  IF (SELECT count(*) FROM public.chat_messages) <> 68
     OR (SELECT count(*) FROM public.chat_sessions) <> 137
     OR (SELECT count(*) FROM public.feedback_signals) <> 5
     OR (SELECT count(*) FROM public.vantage_answer_trace) <> 1046
     OR (SELECT count(*) FROM public.vs_profiles) <> 4 THEN
    RAISE EXCEPTION 'legacy public-table row counts drifted';
  END IF;

  SELECT count(*)::integer INTO external_function_refs
  FROM pg_proc AS routine
  JOIN pg_namespace AS namespace
    ON namespace.oid = routine.pronamespace
  WHERE routine.prokind IN ('f', 'p')
    AND (
      pg_get_functiondef(routine.oid) ILIKE '%chat_messages%'
      OR pg_get_functiondef(routine.oid) ILIKE '%chat_sessions%'
      OR pg_get_functiondef(routine.oid) ILIKE '%feedback_signals%'
      OR pg_get_functiondef(routine.oid) ILIKE '%vantage_answer_trace%'
      OR pg_get_functiondef(routine.oid) ILIKE '%vs_profiles%'
    );
  IF external_function_refs <> 0 THEN
    RAISE EXCEPTION
      'database routines still reference legacy public tables: %',
      external_function_refs;
  END IF;

  SELECT count(*)::integer INTO external_view_refs
  FROM pg_views
  WHERE definition ILIKE '%chat_messages%'
     OR definition ILIKE '%chat_sessions%'
     OR definition ILIKE '%feedback_signals%'
     OR definition ILIKE '%vantage_answer_trace%'
     OR definition ILIKE '%vs_profiles%';
  IF external_view_refs <> 0 THEN
    RAISE EXCEPTION
      'database views still reference legacy public tables: %',
      external_view_refs;
  END IF;

  SELECT count(*)::integer INTO target_triggers
  FROM pg_trigger
  WHERE NOT tgisinternal
    AND tgrelid = ANY(ARRAY[
      'public.chat_messages'::regclass,
      'public.chat_sessions'::regclass,
      'public.feedback_signals'::regclass,
      'public.vantage_answer_trace'::regclass,
      'public.vs_profiles'::regclass
    ]);
  IF target_triggers <> 0 THEN
    RAISE EXCEPTION 'legacy public-table triggers remain: %', target_triggers;
  END IF;

  SELECT count(*)::integer INTO external_foreign_keys
  FROM pg_constraint
  WHERE contype = 'f'
    AND confrelid = ANY(ARRAY[
      'public.chat_messages'::regclass,
      'public.chat_sessions'::regclass,
      'public.feedback_signals'::regclass,
      'public.vantage_answer_trace'::regclass,
      'public.vs_profiles'::regclass
    ])
    AND conrelid <> ALL(ARRAY[
      'public.chat_messages'::regclass,
      'public.chat_sessions'::regclass,
      'public.feedback_signals'::regclass,
      'public.vantage_answer_trace'::regclass,
      'public.vs_profiles'::regclass
    ]);
  IF external_foreign_keys <> 0 THEN
    RAISE EXCEPTION
      'external foreign keys still reference legacy public tables: %',
      external_foreign_keys;
  END IF;

  PERFORM pg_advisory_xact_lock(
    hashtextextended('legacy_public_tables_retirement_v1', 0)
  );
END;
$retirement$;

DROP TABLE public.chat_messages;
DROP TABLE public.chat_sessions;
DROP TABLE public.feedback_signals;
DROP TABLE public.vantage_answer_trace;
DROP TABLE public.vs_profiles;

DO $postcondition$
BEGIN
  IF to_regclass('public.chat_messages') IS NOT NULL
     OR to_regclass('public.chat_sessions') IS NOT NULL
     OR to_regclass('public.feedback_signals') IS NOT NULL
     OR to_regclass('public.vantage_answer_trace') IS NOT NULL
     OR to_regclass('public.vs_profiles') IS NOT NULL THEN
    RAISE EXCEPTION 'legacy public tables remain after retirement';
  END IF;
END;
$postcondition$;

COMMIT;
