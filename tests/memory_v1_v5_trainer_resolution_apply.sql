\set ON_ERROR_STOP on

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

SELECT * FROM memory.preflight_entity_resolution_apply_v5(
  'a8befb71-413b-49b0-a460-c683ea52038e'::uuid,
  '03f72707-bd55-4179-8908-4ef204db7309'::uuid
) \gset trainer_preflight_
SELECT 1 / ((
  :'trainer_preflight_apply_manifest_sha256'
    = 'ad5a31b81d4081583f95bc29a85fcf7de070d59aa535b09e6f3ad9310db53558'
)::integer);

SELECT * FROM memory.apply_entity_resolution_v5(
  '32000000-0000-4000-8000-000000000001'::uuid,
  'a8befb71-413b-49b0-a460-c683ea52038e'::uuid,
  '03f72707-bd55-4179-8908-4ef204db7309'::uuid,
  :'trainer_preflight_apply_manifest_sha256'
) \gset trainer_apply_
SELECT 1 / ((:'trainer_apply_outcome'='applied')::integer);
SELECT 1 / ((:'trainer_apply_bindings_created'::integer=1)::integer);
RESET SESSION AUTHORIZATION;
SELECT 1 / ((
  SELECT count(*)=1
    FROM memory.entity
   WHERE entity_id=:'trainer_apply_applied_entity_id'::uuid
     AND owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
     AND entity_type='concept'
     AND canonical_name='personal trainer'
     AND normalized_name='personal trainer'
     AND metadata->>'identity_state'='named'
     AND metadata->>'resolution_id'
       = 'a8befb71-413b-49b0-a460-c683ea52038e'
)::integer);
SELECT 1 / ((
  SELECT count(*)=1
    FROM memory.entity_alias_observation
   WHERE resolution_id='a8befb71-413b-49b0-a460-c683ea52038e'::uuid
     AND entity_id=:'trainer_apply_applied_entity_id'::uuid
     AND alias_text='personal trainer'
)::integer);
SELECT 1 / ((
  SELECT count(*)=1
    FROM memory.observation_entity_binding
   WHERE observation_id='9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid
     AND subject_entity_id='35029129-27bd-457b-8cb5-82dd37ba32ba'::uuid
     AND object_entity_id=:'trainer_apply_applied_entity_id'::uuid
)::integer);

SET SESSION AUTHORIZATION brains_app;
SELECT * FROM memory.apply_entity_resolution_v5(
  '32000000-0000-4000-8000-000000000001'::uuid,
  'a8befb71-413b-49b0-a460-c683ea52038e'::uuid,
  '03f72707-bd55-4179-8908-4ef204db7309'::uuid,
  :'trainer_preflight_apply_manifest_sha256'
) \gset trainer_replay_
SELECT 1 / ((:'trainer_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'trainer_replay_bindings_created'::integer=0)::integer);
SELECT 1 / ((
  :'trainer_replay_applied_entity_id'=:'trainer_apply_applied_entity_id'
)::integer);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_entity_resolution_apply_v5(
      'a8befb71-413b-49b0-a460-c683ea52038e'::uuid,
      '03f72707-bd55-4179-8908-4ef204db7309'::uuid
    );
    RAISE EXCEPTION 'cross-owner trainer apply preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$isolation$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 1 / ((
  SELECT count(*)=0
    FROM memory.entity
   WHERE metadata->>'resolution_id'
     = 'a8befb71-413b-49b0-a460-c683ea52038e'
)::integer);
SELECT 1 / ((
  SELECT count(*)=0
    FROM memory.entity_resolution_apply
   WHERE resolution_id='a8befb71-413b-49b0-a460-c683ea52038e'::uuid
)::integer);
SELECT 1 / ((
  SELECT count(*)=0
    FROM memory.entity_alias_observation
   WHERE resolution_id='a8befb71-413b-49b0-a460-c683ea52038e'::uuid
)::integer);
SELECT 1 / ((
  SELECT count(*)=0
    FROM memory.observation_entity_binding
   WHERE observation_id='9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid
)::integer);
SELECT 1 / ((
  SELECT count(*)=0
    FROM memory.relational_operation_request
   WHERE request_id='32000000-0000-4000-8000-000000000001'::uuid
)::integer);
SELECT 'memory_v1_v5_trainer_resolution_apply: PASS' AS result;
