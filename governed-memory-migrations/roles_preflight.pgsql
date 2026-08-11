-- Run on the `governed_memory` database after out-of-band database and role
-- provisioning. This file is validation-only: it does not create or alter a
-- database, role, extension, service, or credential.
--
-- Required cluster identities:
--   governed_memory_owner  NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
--   governed_memory_api    LOGIN   NOSUPERUSER NOBYPASSRLS NOINHERIT
--   governed_memory_worker LOGIN   NOSUPERUSER NOBYPASSRLS NOINHERIT
--   memory_ingest_writer   NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
--   memory_erasure_requester NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
-- The deployment principal must be a member of governed_memory_owner and must
-- SET ROLE governed_memory_owner before applying 0001_foundation.
-- Runtime adapters must send transient answer-binding content as bind
-- parameters over PostgreSQL extended query protocol; interpolated SQL is not
-- an accepted deployment shape.

DO $preflight$
DECLARE
  role_record record;
  database_owner oid;
  public_connect boolean := false;
  public_create boolean := false;
  public_temporary boolean := false;
  api_connect boolean := false;
  worker_connect boolean := false;
BEGIN
  IF pg_catalog.current_setting('server_version_num')::integer < 150000 THEN
    RAISE EXCEPTION
      'PostgreSQL 15 or newer is required for core sha256(bytea), '
      'gen_random_uuid(), and UNIQUE NULLS NOT DISTINCT';
  END IF;
  IF current_database() <> 'governed_memory' THEN
    RAISE EXCEPTION 'expected governed_memory database, got %', current_database();
  END IF;
  IF pg_catalog.current_setting('server_encoding') <> 'UTF8' THEN
    RAISE EXCEPTION 'governed_memory requires UTF8 server encoding';
  END IF;
  IF pg_catalog.current_setting('log_parameter_max_length')::integer <> 0
     OR pg_catalog.current_setting(
       'log_parameter_max_length_on_error'
     )::integer <> 0 THEN
    RAISE EXCEPTION
      'bind parameter logging must be disabled for transient Memory content';
  END IF;
  IF pg_catalog.current_setting(
       'auto_explain.log_parameter_max_length', true
     ) IS NOT NULL
     AND pg_catalog.current_setting(
       'auto_explain.log_parameter_max_length', true
     )::integer <> 0 THEN
    RAISE EXCEPTION
      'auto_explain bind parameter logging must be disabled';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_extension AS extension
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = extension.extnamespace
    WHERE extension.extname = 'pgcrypto'
      AND namespace.nspname = 'public'
  ) OR pg_catalog.to_regprocedure('public.digest(bytea,text)') IS NULL THEN
    RAISE EXCEPTION
      'pgcrypto must be provisioned in public for deterministic UUIDv5 SHA-1';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_namespace AS namespace
    CROSS JOIN LATERAL pg_catalog.aclexplode(
      COALESCE(
        namespace.nspacl,
        pg_catalog.acldefault('n', namespace.nspowner)
      )
    ) AS acl
    WHERE namespace.nspname = 'public'
      AND acl.grantee = 0
      AND acl.privilege_type = 'CREATE'
  ) THEN
    RAISE EXCEPTION 'PUBLIC must not retain CREATE on schema public';
  END IF;

  IF current_user <> 'governed_memory_owner' THEN
    RAISE EXCEPTION
      'migration requires current_user governed_memory_owner, got %',
      current_user;
  END IF;

  FOR role_record IN
    SELECT role_name, must_login
    FROM (VALUES
      ('governed_memory_owner'::text, false),
      ('governed_memory_api'::text, true),
      ('governed_memory_worker'::text, true),
      ('memory_ingest_writer'::text, false),
      ('memory_erasure_requester'::text, false)
    ) AS required(role_name, must_login)
  LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_roles AS role
      WHERE role.rolname = role_record.role_name
        AND role.rolcanlogin = role_record.must_login
        AND NOT role.rolsuper
        AND NOT role.rolcreatedb
        AND NOT role.rolcreaterole
        AND NOT role.rolreplication
        AND NOT role.rolbypassrls
        AND NOT role.rolinherit
    ) THEN
      RAISE EXCEPTION 'role % is absent or unsafe', role_record.role_name;
    END IF;
  END LOOP;

  SELECT database.datdba,
         pg_catalog.bool_or(
           acl.grantee = 0 AND acl.privilege_type = 'CONNECT'
         ),
         pg_catalog.bool_or(
           acl.grantee = 0 AND acl.privilege_type = 'CREATE'
         ),
         pg_catalog.bool_or(
           acl.grantee = 0 AND acl.privilege_type = 'TEMPORARY'
         ),
         pg_catalog.bool_or(
           grantee.rolname = 'governed_memory_api'
           AND acl.privilege_type = 'CONNECT'
         ),
         pg_catalog.bool_or(
           grantee.rolname = 'governed_memory_worker'
           AND acl.privilege_type = 'CONNECT'
         )
  INTO database_owner, public_connect, public_create, public_temporary,
       api_connect, worker_connect
  FROM pg_catalog.pg_database AS database
  CROSS JOIN LATERAL pg_catalog.aclexplode(
    coalesce(
      database.datacl,
      pg_catalog.acldefault('d', database.datdba)
    )
  ) AS acl
  LEFT JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
  WHERE database.datname = current_database()
  GROUP BY database.datdba;

  IF database_owner <> pg_catalog.to_regrole('governed_memory_owner')::oid THEN
    RAISE EXCEPTION 'governed_memory_owner does not own the database';
  END IF;
  IF public_connect OR public_create OR public_temporary THEN
    RAISE EXCEPTION 'PUBLIC retains database CONNECT, CREATE, or TEMPORARY';
  END IF;
  IF NOT api_connect OR NOT worker_connect THEN
    RAISE EXCEPTION 'runtime roles lack explicit CONNECT ACL entries';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_database AS database
    CROSS JOIN LATERAL pg_catalog.aclexplode(
      coalesce(
        database.datacl,
        pg_catalog.acldefault('d', database.datdba)
      )
    ) AS acl
    JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
    WHERE database.datname = current_database()
      AND grantee.rolname IN (
        'governed_memory_api', 'governed_memory_worker'
      )
      AND acl.privilege_type <> 'CONNECT'
  ) THEN
    RAISE EXCEPTION 'runtime roles retain non-CONNECT database privileges';
  END IF;
END;
$preflight$;
