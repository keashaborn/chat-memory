\set ON_ERROR_STOP on
\getenv retirement_auditor_password LIFESWITCH_RETIREMENT_AUDITOR_PASSWORD
\if :{?retirement_auditor_password}
\else
\echo 'LIFESWITCH_RETIREMENT_AUDITOR_PASSWORD is required'
\quit 3
\endif

SELECT length(:'retirement_auditor_password') >= 32 AS retirement_auditor_password_valid \gset
\if :retirement_auditor_password_valid
\else
\echo 'LIFESWITCH_RETIREMENT_AUDITOR_PASSWORD must contain at least 32 characters'
\quit 3
\endif

BEGIN;

DO $precondition$
BEGIN
  IF current_database() <> 'memory' OR current_user <> 'sage' THEN
    RAISE EXCEPTION 'legacy retirement auditor target mismatch';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lifeswitch_retirement_auditor') THEN
    RAISE EXCEPTION 'lifeswitch_retirement_auditor already exists; no-clobber provision denied';
  END IF;
  IF to_regclass('memory.assistant_transcript_attestation_v1') IS NULL
     OR to_regclass('memory_ingest_private.memory_ingest_outbox') IS NULL
     OR to_regclass('memory_ingest_private.source_erasure_operation') IS NULL
     OR to_regclass('public.chat_log') IS NULL
     OR to_regclass('chat_integrity.assistant_transcript_attestation_v1') IS NULL THEN
    RAISE EXCEPTION 'legacy retirement evidence relation missing';
  END IF;
END;
$precondition$;

CREATE ROLE lifeswitch_retirement_auditor LOGIN NOINHERIT
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 1 PASSWORD :'retirement_auditor_password';
ALTER ROLE lifeswitch_retirement_auditor SET default_transaction_read_only TO 'on';
ALTER ROLE lifeswitch_retirement_auditor SET statement_timeout TO '30s';
ALTER ROLE lifeswitch_retirement_auditor SET lock_timeout TO '5s';
ALTER ROLE lifeswitch_retirement_auditor SET idle_in_transaction_session_timeout TO '60s';
ALTER ROLE lifeswitch_retirement_auditor SET search_path TO 'pg_catalog';
COMMENT ON ROLE lifeswitch_retirement_auditor IS
  'Bounded read-only identity for content-free legacy memory retirement evidence; no application or administrative authority.';

CREATE OR REPLACE FUNCTION memory.legacy_retirement_evidence_v1()
RETURNS TABLE (
  source_rows bigint,
  eligible_rows bigint,
  quarantine_rows bigint,
  reconciled_rows bigint,
  nonterminal_ingest bigint,
  nonterminal_erasure bigint
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
  WITH counts AS (
    SELECT
      (SELECT count(*) FROM memory.assistant_transcript_attestation_v1) AS source_rows,
      (SELECT count(*)
       FROM memory.assistant_transcript_attestation_v1 AS a
       JOIN public.chat_log AS l
         ON l.id = a.answer_id
        AND l.id = a.chat_log_id
        AND l.owner_user_id = a.owner_user_id
        AND l.thread_id = a.thread_id
        AND pg_catalog.encode(public.digest(l.text, 'sha256'), 'hex') = a.assistant_text_sha256) AS eligible_rows,
      (SELECT count(*)
       FROM memory.assistant_transcript_attestation_v1 AS a
       JOIN public.chat_log AS l
         ON l.id = a.answer_id
        AND l.id = a.chat_log_id
        AND l.owner_user_id = a.owner_user_id
        AND l.thread_id = a.thread_id
        AND pg_catalog.encode(public.digest(l.text, 'sha256'), 'hex') = a.assistant_text_sha256
       JOIN chat_integrity.assistant_transcript_attestation_v1 AS c
         ON c.answer_id = a.answer_id
        AND c.attestation_sha256 = a.attestation_sha256) AS reconciled_rows,
      (SELECT count(*) FROM memory_ingest_private.memory_ingest_outbox WHERE state NOT IN ('completed','skipped')) AS nonterminal_ingest,
      (SELECT count(*) FROM memory_ingest_private.source_erasure_operation WHERE state <> 'completed') AS nonterminal_erasure
  )
  SELECT source_rows, eligible_rows, source_rows - eligible_rows, reconciled_rows,
         nonterminal_ingest, nonterminal_erasure
  FROM counts;
$function$;
ALTER FUNCTION memory.legacy_retirement_evidence_v1() OWNER TO sage;
REVOKE ALL ON FUNCTION memory.legacy_retirement_evidence_v1() FROM PUBLIC;
GRANT USAGE ON SCHEMA memory TO lifeswitch_retirement_auditor;
GRANT EXECUTE ON FUNCTION memory.legacy_retirement_evidence_v1() TO lifeswitch_retirement_auditor;

DO $postcondition$
DECLARE
  auditor_oid oid;
  unexpected_security_definers integer;
BEGIN
  SELECT oid INTO auditor_oid FROM pg_roles WHERE rolname = 'lifeswitch_retirement_auditor';
  IF auditor_oid IS NULL THEN
    RAISE EXCEPTION 'retirement auditor missing after provision';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE oid = auditor_oid AND (
      NOT rolcanlogin OR rolsuper OR rolinherit OR rolcreaterole OR rolcreatedb
      OR rolreplication OR rolbypassrls OR rolconnlimit <> 1
    )
  ) THEN
    RAISE EXCEPTION 'retirement auditor attributes violate boundary';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_auth_members WHERE member = auditor_oid) THEN
    RAISE EXCEPTION 'retirement auditor must not inherit role memberships';
  END IF;
  IF NOT has_function_privilege('lifeswitch_retirement_auditor', 'memory.legacy_retirement_evidence_v1()', 'EXECUTE') THEN
    RAISE EXCEPTION 'retirement evidence function is not executable';
  END IF;
  IF has_table_privilege('lifeswitch_retirement_auditor', 'memory.assistant_transcript_attestation_v1', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
     OR has_table_privilege('lifeswitch_retirement_auditor', 'public.chat_log', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
     OR has_table_privilege('lifeswitch_retirement_auditor', 'chat_integrity.assistant_transcript_attestation_v1', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
     OR has_table_privilege('lifeswitch_retirement_auditor', 'memory_ingest_private.memory_ingest_outbox', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
     OR has_table_privilege('lifeswitch_retirement_auditor', 'memory_ingest_private.source_erasure_operation', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
     OR has_any_column_privilege('lifeswitch_retirement_auditor', 'memory.assistant_transcript_attestation_v1', 'SELECT,INSERT,UPDATE')
     OR has_any_column_privilege('lifeswitch_retirement_auditor', 'public.chat_log', 'SELECT,INSERT,UPDATE')
     OR has_any_column_privilege('lifeswitch_retirement_auditor', 'chat_integrity.assistant_transcript_attestation_v1', 'SELECT,INSERT,UPDATE')
     OR has_any_column_privilege('lifeswitch_retirement_auditor', 'memory_ingest_private.memory_ingest_outbox', 'SELECT,INSERT,UPDATE')
     OR has_any_column_privilege('lifeswitch_retirement_auditor', 'memory_ingest_private.source_erasure_operation', 'SELECT,INSERT,UPDATE') THEN
    RAISE EXCEPTION 'retirement auditor has forbidden direct evidence-table authority';
  END IF;
  IF has_schema_privilege('lifeswitch_retirement_auditor', 'memory', 'CREATE')
     OR has_schema_privilege('lifeswitch_retirement_auditor', 'memory_ingest_private', 'CREATE')
     OR has_schema_privilege('lifeswitch_retirement_auditor', 'chat_integrity', 'CREATE')
     OR has_schema_privilege('lifeswitch_retirement_auditor', 'public', 'CREATE') THEN
    RAISE EXCEPTION 'retirement auditor has forbidden persistent schema-create authority';
  END IF;
  SELECT count(*)::integer INTO unexpected_security_definers
  FROM pg_proc AS p
  JOIN pg_namespace AS n ON n.oid = p.pronamespace
  WHERE p.prosecdef
    AND n.nspname NOT IN ('pg_catalog','information_schema')
    AND p.oid <> 'memory.legacy_retirement_evidence_v1()'::regprocedure
    AND has_function_privilege('lifeswitch_retirement_auditor', p.oid, 'EXECUTE');
  IF unexpected_security_definers <> 0 THEN
    RAISE EXCEPTION 'unexpected executable security-definer function: %', unexpected_security_definers;
  END IF;
  IF current_setting('default_transaction_read_only') <> 'off' THEN
    RAISE EXCEPTION 'administrator session state unexpectedly changed';
  END IF;
END;
$postcondition$;

COMMIT;
