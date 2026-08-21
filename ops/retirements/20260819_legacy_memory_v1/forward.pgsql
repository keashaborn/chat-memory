BEGIN;

DO $retirement$
DECLARE
  source_rows integer;
  eligible_rows integer;
  reconciled_rows integer;
  external_function_refs integer;
  external_view_refs integer;
  external_materialized_view_refs integer;
  external_trigger_refs integer;
  external_foreign_keys integer;
  external_catalog_dependencies integer;
  proof_name text;
BEGIN
  IF current_user <> 'sage' OR current_database() <> 'memory' THEN
    RAISE EXCEPTION 'legacy memory retirement target mismatch';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('legacy_memory_schema_retirement_v1', 0));

  IF COALESCE(current_setting('lifeswitch.legacy_memory_backup_sha256', true), '') <> '0adc9bcaba1baee76b815aeb3000ce3c0685367648e976c138354145668afa15'
     OR COALESCE(current_setting('lifeswitch.legacy_memory_restore_receipt_sha256', true), '') <> '1f5a117b4dd83176e34141340c9fd9eede8623f1319d8cfd260fbf5f37cb9e24' THEN
    RAISE EXCEPTION 'exact legacy memory backup and disposable restore receipt are required';
  END IF;
  FOREACH proof_name IN ARRAY ARRAY[
    'lifeswitch.legacy_attestation_reconciliation_receipt_sha256',
    'lifeswitch.legacy_attestation_quarantine_ciphertext_sha256',
    'lifeswitch.legacy_attestation_key_custody_receipt_sha256',
    'lifeswitch.legacy_memory_dependency_catalog_sha256'
  ] LOOP
    IF COALESCE(current_setting(proof_name, true), '') !~ '^[0-9a-f]{64}$' THEN
      RAISE EXCEPTION 'required hash-bound proof missing: %', proof_name;
    END IF;
  END LOOP;
  IF COALESCE(current_setting('lifeswitch.legacy_attestation_source_sha256', true), '') <> 'c07967305707d83e258bd441939359b472b95bb414eeb739a7edd99a5f657831'
     OR COALESCE(current_setting('lifeswitch.legacy_attestation_eligible_sha256', true), '') <> '4b3917cac82fc5a4522a2bb8e0a9296651daedc3d94fb6b02840d9b5f6b64678'
     OR COALESCE(current_setting('lifeswitch.legacy_attestation_quarantine_sha256', true), '') <> '38f20b7df5cbd1d3a8d413825eb76a6a7a682f0e78b491e1aa09a315113d90e1' THEN
    RAISE EXCEPTION 'legacy attestation classification proof mismatch';
  END IF;
  IF (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='memory' AND c.relkind='r') <> 160
     OR (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='memory_ingest_private' AND c.relkind='r') <> 7 THEN
    RAISE EXCEPTION 'legacy memory schema shape drifted';
  END IF;
  IF (SELECT count(*) FROM memory_ingest_private.memory_ingest_outbox WHERE state NOT IN ('completed','skipped')) <> 0
     OR (SELECT count(*) FROM memory_ingest_private.source_erasure_operation WHERE state <> 'completed') <> 0 THEN
    RAISE EXCEPTION 'legacy memory queues are not terminal';
  END IF;
  IF (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname = ANY(ARRAY['chat_attachments_serialize_source_erasure','chat_log_serialize_source_erasure','threads_serialize_source_erasure','response_transcript_serialize_source_erasure'])) <> 0 THEN
    RAISE EXCEPTION 'legacy source-erasure triggers remain';
  END IF;
  IF to_regclass('conversation_sync_private.zep_turn_outbox') IS NULL
     OR to_regprocedure('chat_history_private.clear_history(uuid,text,uuid,integer)') IS NULL
     OR to_regprocedure('chat_history_private.clear_message_tail(uuid,uuid)') IS NULL
     OR pg_get_functiondef(to_regprocedure('chat_history_private.clear_history(uuid,text,uuid,integer)')) NOT ILIKE '%conversation_sync_private.zep_turn_outbox%'
     OR pg_get_functiondef(to_regprocedure('chat_history_private.clear_message_tail(uuid,uuid)')) NOT ILIKE '%conversation_sync_private.zep_turn_outbox%'
     OR pg_get_functiondef(to_regprocedure('chat_history_private.clear_history(uuid,text,uuid,integer)')) ILIKE '%memory_ingest_private.%'
     OR pg_get_functiondef(to_regprocedure('chat_history_private.clear_message_tail(uuid,uuid)')) ILIKE '%memory_ingest_private.%' THEN
    RAISE EXCEPTION 'canonical Zep erasure boundary is not active';
  END IF;

  SELECT count(*)::integer INTO source_rows FROM memory.assistant_transcript_attestation_v1;
  SELECT count(*)::integer INTO eligible_rows
  FROM memory.assistant_transcript_attestation_v1 a
  JOIN public.chat_log l ON l.id=a.answer_id AND l.id=a.chat_log_id AND l.owner_user_id=a.owner_user_id AND l.thread_id=a.thread_id AND pg_catalog.encode(public.digest(l.text,'sha256'),'hex')=a.assistant_text_sha256;
  SELECT count(*)::integer INTO reconciled_rows
  FROM memory.assistant_transcript_attestation_v1 a
  JOIN public.chat_log l ON l.id=a.answer_id AND l.id=a.chat_log_id AND l.owner_user_id=a.owner_user_id AND l.thread_id=a.thread_id AND pg_catalog.encode(public.digest(l.text,'sha256'),'hex')=a.assistant_text_sha256
  JOIN chat_integrity.assistant_transcript_attestation_v1 c ON c.answer_id=a.answer_id AND c.attestation_sha256=a.attestation_sha256;
  IF source_rows <> 190 OR eligible_rows <> 32 OR source_rows - eligible_rows <> 158 OR reconciled_rows <> 32 THEN
    RAISE EXCEPTION 'legacy attestation reconciliation is incomplete';
  END IF;

  SELECT count(*)::integer INTO external_function_refs FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE p.prokind IN ('f','p') AND n.nspname NOT IN ('memory','memory_ingest_private') AND (pg_get_functiondef(p.oid) ILIKE '%memory.%' OR pg_get_functiondef(p.oid) ILIKE '%memory_ingest_private.%');
  SELECT count(*)::integer INTO external_view_refs FROM pg_views WHERE schemaname NOT IN ('memory','memory_ingest_private') AND (definition ILIKE '%memory.%' OR definition ILIKE '%memory_ingest_private.%');
  SELECT count(*)::integer INTO external_materialized_view_refs FROM pg_matviews WHERE schemaname NOT IN ('memory','memory_ingest_private') AND (definition ILIKE '%memory.%' OR definition ILIKE '%memory_ingest_private.%');
  SELECT count(*)::integer INTO external_trigger_refs FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal AND n.nspname NOT IN ('memory','memory_ingest_private') AND (pg_get_triggerdef(t.oid) ILIKE '%memory.%' OR pg_get_triggerdef(t.oid) ILIKE '%memory_ingest_private.%');
  SELECT count(*)::integer INTO external_foreign_keys FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_class r ON r.oid=k.confrelid JOIN pg_namespace rn ON rn.oid=r.relnamespace WHERE k.contype='f' AND ((n.nspname IN ('memory','memory_ingest_private') AND rn.nspname NOT IN ('memory','memory_ingest_private')) OR (rn.nspname IN ('memory','memory_ingest_private') AND n.nspname NOT IN ('memory','memory_ingest_private')));
  SELECT count(*)::integer INTO external_catalog_dependencies FROM pg_depend d CROSS JOIN LATERAL pg_identify_object(d.refclassid,d.refobjid,d.refobjsubid) ref CROSS JOIN LATERAL pg_identify_object(d.classid,d.objid,d.objsubid) dep WHERE ref.schema IN ('memory','memory_ingest_private') AND dep.schema IS NOT NULL AND dep.schema NOT IN ('memory','memory_ingest_private') AND d.deptype IN ('n','a');
  IF external_function_refs <> 0 OR external_view_refs <> 0 OR external_materialized_view_refs <> 0 OR external_trigger_refs <> 0 OR external_foreign_keys <> 0 OR external_catalog_dependencies <> 0 THEN
    RAISE EXCEPTION 'external legacy dependencies remain: functions %, views %, matviews %, triggers %, foreign_keys %, catalog %', external_function_refs, external_view_refs, external_materialized_view_refs, external_trigger_refs, external_foreign_keys, external_catalog_dependencies;
  END IF;
END;
$retirement$;

DROP SCHEMA memory_ingest_private CASCADE;
DROP SCHEMA memory CASCADE;

DO $postcondition$
BEGIN
  IF to_regnamespace('memory_ingest_private') IS NOT NULL OR to_regnamespace('memory') IS NOT NULL THEN
    RAISE EXCEPTION 'legacy memory schemas remain after retirement';
  END IF;
  IF to_regclass('conversation_sync_private.zep_turn_outbox') IS NULL
     OR to_regprocedure('chat_history_private.clear_history(uuid,text,uuid,integer)') IS NULL
     OR to_regprocedure('chat_history_private.clear_message_tail(uuid,uuid)') IS NULL THEN
    RAISE EXCEPTION 'canonical conversation boundary changed during retirement';
  END IF;
END;
$postcondition$;

COMMIT;
