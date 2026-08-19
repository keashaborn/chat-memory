BEGIN;

DO $retirement$
DECLARE
  expected_database text;
  backup_sha256 text;
  restore_receipt_sha256 text;
  memory_tables integer;
  ingest_tables integer;
  external_function_refs integer;
  legacy_triggers integer;
  nonterminal_ingest integer;
  nonterminal_erasure integer;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'legacy memory retirement must run as sage, current_user=%',
      current_user;
  END IF;

  expected_database := COALESCE(
    NULLIF(current_setting(
      'lifeswitch.legacy_memory_target_database', true
    ), ''),
    'memory'
  );
  IF current_database() <> expected_database THEN
    RAISE EXCEPTION
      'legacy memory retirement target mismatch: expected %, current %',
      expected_database, current_database();
  END IF;

  backup_sha256 := COALESCE(current_setting(
    'lifeswitch.legacy_memory_backup_sha256', true
  ), '');
  restore_receipt_sha256 := COALESCE(current_setting(
    'lifeswitch.legacy_memory_restore_receipt_sha256', true
  ), '');
  IF backup_sha256 !~ '^[0-9a-f]{64}$'
     OR restore_receipt_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION
      'hash-bound backup and restore receipt are required';
  END IF;
  IF COALESCE(current_setting(
    'lifeswitch.legacy_memory_cron_retired', true
  ), '') <> 'on' THEN
    RAISE EXCEPTION 'legacy memory cron retirement proof is required';
  END IF;

  IF to_regclass(
       'conversation_sync_private.zep_turn_outbox'
     ) IS NULL
     OR to_regprocedure(
       'ai_operations.enforce_telemetry_retention_v1()'
     ) IS NULL THEN
    RAISE EXCEPTION
      'canonical Zep synchronization and telemetry owners are required';
  END IF;

  SELECT count(*)::integer INTO memory_tables
  FROM pg_class AS relation
  JOIN pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
  WHERE namespace.nspname = 'memory'
    AND relation.relkind = 'r';
  SELECT count(*)::integer INTO ingest_tables
  FROM pg_class AS relation
  JOIN pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
  WHERE namespace.nspname = 'memory_ingest_private'
    AND relation.relkind = 'r';
  IF memory_tables <> 160 OR ingest_tables <> 7 THEN
    RAISE EXCEPTION
      'legacy memory object shape drifted: memory tables %, ingest tables %',
      memory_tables, ingest_tables;
  END IF;

  SELECT count(*)::integer INTO external_function_refs
  FROM pg_proc AS routine
  JOIN pg_namespace AS namespace
    ON namespace.oid = routine.pronamespace
  WHERE routine.prokind IN ('f', 'p')
    AND namespace.nspname NOT IN ('memory', 'memory_ingest_private')
    AND pg_get_functiondef(routine.oid)
      ILIKE '%memory_ingest_private%';
  IF external_function_refs <> 0 THEN
    RAISE EXCEPTION
      'external routines still reference legacy memory: %',
      external_function_refs;
  END IF;

  SELECT count(*)::integer INTO legacy_triggers
  FROM pg_trigger
  WHERE NOT tgisinternal
    AND tgname = ANY(ARRAY[
      'chat_attachments_serialize_source_erasure',
      'chat_log_serialize_source_erasure',
      'threads_serialize_source_erasure',
      'response_transcript_serialize_source_erasure'
    ]);
  IF legacy_triggers <> 0 THEN
    RAISE EXCEPTION
      'legacy source-erasure triggers remain: %', legacy_triggers;
  END IF;

  SELECT count(*)::integer INTO nonterminal_ingest
  FROM memory_ingest_private.memory_ingest_outbox
  WHERE state NOT IN ('completed', 'skipped');
  SELECT count(*)::integer INTO nonterminal_erasure
  FROM memory_ingest_private.source_erasure_operation
  WHERE state <> 'completed';
  IF nonterminal_ingest <> 0 OR nonterminal_erasure <> 0 THEN
    RAISE EXCEPTION
      'legacy memory work remains nonterminal: ingest %, erasure %',
      nonterminal_ingest, nonterminal_erasure;
  END IF;

  PERFORM pg_advisory_xact_lock(
    hashtextextended('legacy_memory_schema_retirement_v1', 0)
  );
END;
$retirement$;

DROP SCHEMA memory_ingest_private CASCADE;
DROP SCHEMA memory CASCADE;

DO $postcondition$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_namespace
    WHERE nspname IN ('memory', 'memory_ingest_private')
  ) THEN
    RAISE EXCEPTION 'legacy memory schemas remain after retirement';
  END IF;
END;
$postcondition$;

COMMIT;
