\set ON_ERROR_STOP on

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

SELECT * FROM memory.preflight_entity_resolution_apply_v5(
  'fc1859aa-f280-4aa1-b75a-ee882e969c87'::uuid,NULL
) \gset self_preflight_
SELECT 1 / ((
  :'self_preflight_apply_manifest_sha256'
    = '5e9960a36d27a6b50b50ede71dd43f49fc02d45522189fa57d304e86b7a3f479'
)::integer);

SELECT * FROM memory.preflight_entity_resolution_review_v5(
  'a8befb71-413b-49b0-a460-c683ea52038e'::uuid,
  'approved'::memory.entity_review_decision,
  'source explicitly states personal trainer; approve governed concept creation'
) \gset trainer_preflight_
SELECT 1 / ((
  :'trainer_preflight_authorization_manifest_sha256'
    = '2b046df82c08e71863882705536763263d17c646aba7abe0c0dfa8556e1d0cfe'
)::integer);

SELECT * FROM memory.apply_entity_resolution_v5(
  '31000000-0000-4000-8000-000000000001'::uuid,
  'fc1859aa-f280-4aa1-b75a-ee882e969c87'::uuid,
  NULL,
  :'self_preflight_apply_manifest_sha256'
) \gset self_apply_
SELECT 1 / ((:'self_apply_outcome'='applied')::integer);
SELECT 1 / ((:'self_apply_bindings_created'::integer=0)::integer);
SELECT 1 / ((
  :'self_apply_applied_entity_id'='35029129-27bd-457b-8cb5-82dd37ba32ba'
)::integer);

SELECT * FROM memory.review_entity_resolution_v5(
  '31000000-0000-4000-8000-000000000002'::uuid,
  'a8befb71-413b-49b0-a460-c683ea52038e'::uuid,
  'approved'::memory.entity_review_decision,
  'source explicitly states personal trainer; approve governed concept creation',
  :'trainer_preflight_authorization_manifest_sha256'
) \gset trainer_review_
SELECT 1 / ((:'trainer_review_outcome'='applied')::integer);

SELECT * FROM memory.apply_entity_resolution_v5(
  '31000000-0000-4000-8000-000000000001'::uuid,
  'fc1859aa-f280-4aa1-b75a-ee882e969c87'::uuid,
  NULL,
  :'self_preflight_apply_manifest_sha256'
) \gset self_replay_
SELECT 1 / ((:'self_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'self_replay_bindings_created'::integer=0)::integer);

SELECT * FROM memory.review_entity_resolution_v5(
  '31000000-0000-4000-8000-000000000002'::uuid,
  'a8befb71-413b-49b0-a460-c683ea52038e'::uuid,
  'approved'::memory.entity_review_decision,
  'source explicitly states personal trainer; approve governed concept creation',
  :'trainer_preflight_authorization_manifest_sha256'
) \gset trainer_replay_
SELECT 1 / ((:'trainer_replay_outcome'='replayed')::integer);
SELECT 1 / ((
  :'trainer_replay_review_id'=:'trainer_review_review_id'
)::integer);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_entity_resolution_apply_v5(
      'fc1859aa-f280-4aa1-b75a-ee882e969c87'::uuid,NULL
    );
    RAISE EXCEPTION 'cross-owner self apply preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
  BEGIN
    PERFORM * FROM memory.preflight_entity_resolution_review_v5(
      'a8befb71-413b-49b0-a460-c683ea52038e'::uuid,
      'approved'::memory.entity_review_decision,
      'source explicitly states personal trainer; approve governed concept creation'
    );
    RAISE EXCEPTION 'cross-owner trainer review preflight unexpectedly succeeded';
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
    FROM memory.entity_resolution_review
   WHERE resolution_id='a8befb71-413b-49b0-a460-c683ea52038e'::uuid
)::integer);
SELECT 1 / ((
  SELECT count(*)=0
    FROM memory.relational_operation_request
   WHERE request_id IN (
     '31000000-0000-4000-8000-000000000001'::uuid,
     '31000000-0000-4000-8000-000000000002'::uuid
   )
)::integer);
SELECT 'memory_v1_v5_initial_resolution_apply: PASS' AS result;
