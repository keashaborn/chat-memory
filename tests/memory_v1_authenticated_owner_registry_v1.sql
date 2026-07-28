\set ON_ERROR_STOP on
BEGIN;

DO $prerequisites$
BEGIN
  IF to_regclass('memory.authenticated_owner_registry_v1') IS NULL
     OR to_regprocedure(
       'memory.register_authenticated_owner_v1(uuid,text,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.list_recent_authenticated_owners_v1(interval,integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'authenticated owner registry objects are absent';
  END IF;
  IF (
    SELECT count(*)
    FROM memory.authenticated_owner_registry_v1
    WHERE auth_contract_version='current_account_bootstrap_20260727'
  ) <> 6 THEN
    RAISE EXCEPTION 'current account bootstrap is not exact';
  END IF;
  IF has_table_privilege(
    'brains_app',
    'memory.authenticated_owner_registry_v1',
    'SELECT'
  ) THEN
    RAISE EXCEPTION 'brains_app has forbidden direct registry access';
  END IF;
END
$prerequisites$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id',
  '11111111-1111-4111-8111-111111111111',
  true
);

DO $register$
DECLARE
  outcome text;
BEGIN
  SELECT memory.register_authenticated_owner_v1(
    '11111111-1111-4111-8111-111111111111',
    'supabase_access_token_v1',
    'registry-test-request'
  ) INTO outcome;
  IF outcome <> 'registered' THEN
    RAISE EXCEPTION 'new authenticated owner was not registered';
  END IF;
END
$register$;

DO $cross_owner$
BEGIN
  BEGIN
    PERFORM memory.register_authenticated_owner_v1(
      '22222222-2222-4222-8222-222222222222',
      'supabase_access_token_v1',
      'cross-owner-request'
    );
    RAISE EXCEPTION 'cross-owner registration unexpectedly succeeded';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END
$cross_owner$;

DO $listing$
DECLARE
  owners uuid[];
BEGIN
  SELECT array_agg(owner_user_id ORDER BY owner_user_id)
  INTO owners
  FROM memory.list_recent_authenticated_owners_v1(interval '90 days',1000);
  IF NOT (
    '11111111-1111-4111-8111-111111111111'::uuid = ANY(owners)
  ) THEN
    RAISE EXCEPTION 'registered authenticated owner is not discoverable';
  END IF;
  IF (
    '99999999-9999-4999-8999-999999999999'::uuid = ANY(owners)
  ) THEN
    RAISE EXCEPTION 'unregistered historical owner was discovered';
  END IF;
END
$listing$;

RESET SESSION AUTHORIZATION;
ROLLBACK;
