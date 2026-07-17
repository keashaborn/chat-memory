\set ON_ERROR_STOP on

BEGIN;
SET SESSION AUTHORIZATION brains_app;

DO $missing_actor$
BEGIN
  BEGIN
    PERFORM * FROM memory.read_v5_shadow_claims(
      ARRAY['50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid]
    );
    RAISE EXCEPTION 'missing-actor V5 read unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$missing_actor$;

SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

DO $direct_table_denial$
BEGIN
  BEGIN
    PERFORM 1 FROM memory.observation LIMIT 1;
    RAISE EXCEPTION 'brains_app directly read a protected V5 table';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$direct_table_denial$;

SELECT 1 / (((
  SELECT count(*)
    FROM memory.read_v5_shadow_claims(
      ARRAY['50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid]
    ) AS value
   WHERE value.owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
     AND value.status='candidate'
     AND value.predicate='occupation.works_as'
     AND value.projection_review_decision='authorized'
     AND value.projection_apply_outcome='applied'
     AND value.evidence_by_stance->'supports'
           ? 'fca9e5dc-83c2-4456-8db8-1fe6102eb74d'
     AND value.evidence_by_stance->'opposes'='[]'::jsonb
     AND value.observation_ids
           @> ARRAY['9bf1e6b2-1840-4524-98dc-142567ebe013']
     AND value.valid_from='2026-07-14T17:15:20.3846+00'::timestamptz
     AND value.valid_to IS NULL
)=1)::integer);

DO $invalid_arrays$
BEGIN
  BEGIN
    PERFORM * FROM memory.read_v5_shadow_claims(ARRAY[]::uuid[]);
    RAISE EXCEPTION 'empty V5 candidate array unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN
    NULL;
  END;
  BEGIN
    PERFORM * FROM memory.read_v5_shadow_claims(
      ARRAY[
        '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
        '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
      ]
    );
    RAISE EXCEPTION 'duplicate V5 candidate array unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN
    NULL;
  END;
END
$invalid_arrays$;

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT 1 / (((
  SELECT count(*)
    FROM memory.read_v5_shadow_claims(
      ARRAY['50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid]
    )
)=0)::integer);

RESET SESSION AUTHORIZATION;
ROLLBACK;

DO $maintenance_denial$
BEGIN
  BEGIN
    PERFORM * FROM memory.read_v5_shadow_claims(
      ARRAY['50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid]
    );
    RAISE EXCEPTION 'maintenance-session V5 read unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$maintenance_denial$;

SELECT 1 / (((
  SELECT count(*)
    FROM information_schema.role_table_grants
   WHERE grantee='brains_app'
     AND table_schema='memory'
     AND table_name IN (
       'claim_observation','observation','observation_temporal',
       'projection_apply_event'
     )
)=0)::integer);

SELECT 1 / (((
  SELECT count(*)
    FROM pg_policies
   WHERE schemaname='memory'
     AND policyname='owner_isolation_v5_reader'
     AND roles=ARRAY['memory_v5_reader']::name[]
)=4)::integer);

SELECT 1 / (((
  SELECT count(*)
    FROM pg_roles
   WHERE rolname='memory_v5_reader'
     AND NOT rolcanlogin
     AND NOT rolinherit
     AND NOT rolbypassrls
     AND NOT rolsuper
     AND NOT rolcreatedb
     AND NOT rolcreaterole
)=1)::integer);

SELECT 1 / (((
  SELECT provolatile
    FROM pg_proc
   WHERE oid='memory.read_v5_shadow_claims(uuid[])'::regprocedure
)='s')::integer);

SELECT 'memory_v1_v5_shadow_read_api: PASS' AS result;
