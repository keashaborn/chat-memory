BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SET LOCAL idle_in_transaction_session_timeout = '60s';

DO $rollback_preflight$
DECLARE
  role_is_super boolean;
  role_bypasses_rls boolean;
  table_owner text;
  rls_enabled boolean;
  rls_forced boolean;
  unexpected_grantee text;
BEGIN
  SELECT rolsuper, rolbypassrls
  INTO role_is_super, role_bypasses_rls
  FROM pg_roles
  WHERE rolname = 'brains_app';

  IF NOT FOUND THEN
    RAISE EXCEPTION 'required role brains_app is missing';
  END IF;
  IF role_is_super OR role_bypasses_rls THEN
    RAISE EXCEPTION
      'brains_app must not be superuser or bypass RLS';
  END IF;

  SELECT
    pg_get_userbyid(c.relowner),
    c.relrowsecurity,
    c.relforcerowsecurity
  INTO table_owner, rls_enabled, rls_forced
  FROM pg_class AS c
  WHERE c.oid = to_regclass('trusted_web.response_transcript_v1');

  IF NOT FOUND THEN
    RAISE EXCEPTION
      'required table trusted_web.response_transcript_v1 is missing';
  END IF;
  IF table_owner <> 'brains_app' THEN
    RAISE EXCEPTION
      'unexpected response_transcript_v1 owner: %', table_owner;
  END IF;
  IF NOT rls_enabled OR NOT rls_forced THEN
    RAISE EXCEPTION
      'response_transcript_v1 must have enabled and forced RLS';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'trusted_web'
      AND tablename = 'response_transcript_v1'
      AND policyname = 'response_transcript_owner_policy'
      AND roles::text[] = ARRAY['brains_app']::text[]
  ) THEN
    RAISE EXCEPTION
      'expected brains_app-only policy is not installed';
  END IF;

  SELECT grantee
  INTO unexpected_grantee
  FROM information_schema.table_privileges
  WHERE table_schema = 'trusted_web'
    AND table_name = 'response_transcript_v1'
    AND grantee <> 'brains_app'
  ORDER BY grantee
  LIMIT 1;

  IF FOUND THEN
    RAISE EXCEPTION
      'unexpected response_transcript_v1 grantee: %',
      unexpected_grantee;
  END IF;
END
$rollback_preflight$;

LOCK TABLE trusted_web.response_transcript_v1
  IN ACCESS EXCLUSIVE MODE;

DROP POLICY IF EXISTS response_transcript_owner_policy
  ON trusted_web.response_transcript_v1;

CREATE POLICY response_transcript_owner_policy
  ON trusted_web.response_transcript_v1
  USING (
    owner_user_id = NULLIF(
      current_setting('app.user_id', true),
      ''
    )::uuid
  )
  WITH CHECK (
    owner_user_id = NULLIF(
      current_setting('app.user_id', true),
      ''
    )::uuid
  );

REVOKE ALL ON trusted_web.response_transcript_v1 FROM PUBLIC;

COMMIT;
