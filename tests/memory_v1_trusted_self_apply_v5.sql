\set ON_ERROR_STOP on

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

SELECT * FROM memory.preflight_entity_resolution_apply_v5(
  'fc1859aa-f280-4aa1-b75a-ee882e969c87'::uuid,NULL
) \gset self_preflight_

SELECT * FROM memory.apply_entity_resolution_v5(
  '30000000-0000-4000-8000-000000000001'::uuid,
  'fc1859aa-f280-4aa1-b75a-ee882e969c87'::uuid,
  NULL,
  :'self_preflight_apply_manifest_sha256'
) \gset self_apply_

SELECT 1 / ((:'self_apply_outcome'='applied')::integer);
SELECT 1 / ((:'self_apply_bindings_created'::integer=0)::integer);
SELECT 1 / ((
  :'self_apply_applied_entity_id'='35029129-27bd-457b-8cb5-82dd37ba32ba'
)::integer);

SELECT * FROM memory.apply_entity_resolution_v5(
  '30000000-0000-4000-8000-000000000001'::uuid,
  'fc1859aa-f280-4aa1-b75a-ee882e969c87'::uuid,
  NULL,
  :'self_preflight_apply_manifest_sha256'
) \gset self_replay_

SELECT 1 / ((:'self_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'self_replay_bindings_created'::integer=0)::integer);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_entity_resolution_apply_v5(
      'fc1859aa-f280-4aa1-b75a-ee882e969c87'::uuid,NULL
    );
    RAISE EXCEPTION 'cross-owner self-resolution preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$isolation$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 1 / ((
  SELECT count(*)=0
    FROM memory.entity_resolution_apply
   WHERE resolution_id='fc1859aa-f280-4aa1-b75a-ee882e969c87'::uuid
)::integer);
SELECT 1 / ((
  SELECT count(*)=0
    FROM memory.relational_operation_request
   WHERE request_id='30000000-0000-4000-8000-000000000001'::uuid
)::integer);
SELECT 'memory_v1_trusted_self_apply_v5: PASS' AS result;
