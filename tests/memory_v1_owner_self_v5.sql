\set ON_ERROR_STOP on

CREATE OR REPLACE FUNCTION pg_temp.assert_missing_actor_denied()
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  PERFORM * FROM memory.preflight_owner_self_v5();
  RAISE EXCEPTION 'missing actor unexpectedly accepted';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END
$$;

CREATE OR REPLACE FUNCTION pg_temp.assert_duplicate_self_denied(owner_id uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  INSERT INTO memory.entity(
    owner_user_id,entity_key,entity_type,canonical_name,normalized_name
  ) VALUES(owner_id,gen_random_uuid()::text,'self','Self','self');
  RAISE EXCEPTION 'duplicate active self unexpectedly accepted';
EXCEPTION WHEN unique_violation THEN NULL;
END
$$;

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','',true);
SELECT pg_temp.assert_missing_actor_denied();

SELECT set_config(
  'app.user_id','11111111-1111-4111-8111-111111111111',true
);
SELECT * FROM memory.preflight_owner_self_v5() \gset before_
SELECT 1 / ((:'before_state'='create')::integer);
SELECT * FROM memory.bootstrap_owner_self_v5(
  '10000000-0000-4000-8000-000000000001',
  :'before_authorization_manifest_sha256'
) \gset created_
SELECT 1 / ((:'created_outcome'='created')::integer);
SELECT 1 / ((:'created_rows_written'::integer=2)::integer);
SELECT 1 / ((
  SELECT count(*)=1 FROM memory.entity
   WHERE owner_user_id='11111111-1111-4111-8111-111111111111'
     AND entity_type='self' AND status='active'
)::integer);
SELECT 1 / ((
  SELECT entity_key !~ '11111111' FROM memory.entity
   WHERE entity_id=:'created_entity_id'
)::integer);

SELECT * FROM memory.bootstrap_owner_self_v5(
  '10000000-0000-4000-8000-000000000001',
  :'before_authorization_manifest_sha256'
) \gset replay_
SELECT 1 / ((:'replay_outcome'='replayed')::integer);
SELECT 1 / ((:'replay_rows_written'::integer=0)::integer);
SELECT pg_temp.assert_duplicate_self_denied(
  '11111111-1111-4111-8111-111111111111'
);

SELECT set_config(
  'app.user_id','22222222-2222-4222-8222-222222222222',true
);
SELECT 1 / ((
  SELECT count(*)=0 FROM memory.entity
   WHERE owner_user_id='11111111-1111-4111-8111-111111111111'
)::integer);
SELECT * FROM memory.preflight_owner_self_v5() \gset second_
SELECT * FROM memory.bootstrap_owner_self_v5(
  '20000000-0000-4000-8000-000000000001',
  :'second_authorization_manifest_sha256'
) \gset second_created_
SELECT 1 / ((:'second_created_outcome'='created')::integer);

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 1 / ((SELECT count(*)=0 FROM memory.owner_self_bootstrap_v5)::integer);
SELECT 1 / ((
  SELECT count(*)=0 FROM memory.entity WHERE entity_type='self'
)::integer);
SELECT 'memory_v1_owner_self_v5: PASS' AS result;
