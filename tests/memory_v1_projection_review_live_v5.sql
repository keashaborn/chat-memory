\set ON_ERROR_STOP on

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT * FROM memory.review_projection_v5(
  '33000000-0000-4000-8000-000000000001'::uuid,
  'p01',
  'authorized'::memory.projection_review_decision_v5,
  'system',
  'memory_v1_v5_entity_resolution_projection_preparation_20260716',
  'direct user statement with applied owner-scoped entity bindings; phase-authorized projection preparation',
  '["direct_user_statement","applied_entity_bindings","phase_authorized_projection_preparation"]'::jsonb,
  'd78fadfe2334a95f77d2f11c549d65866f72fd4e522b25b0cd9c20ad8bab6f50'
) \gset review_
SELECT 1 / ((:'review_outcome'='applied')::integer);
SELECT 1 / ((:'review_rows_written'::integer=1)::integer);

SELECT * FROM memory.review_projection_v5(
  '33000000-0000-4000-8000-000000000001'::uuid,
  'p01',
  'authorized'::memory.projection_review_decision_v5,
  'system',
  'memory_v1_v5_entity_resolution_projection_preparation_20260716',
  'direct user statement with applied owner-scoped entity bindings; phase-authorized projection preparation',
  '["direct_user_statement","applied_entity_bindings","phase_authorized_projection_preparation"]'::jsonb,
  'd78fadfe2334a95f77d2f11c549d65866f72fd4e522b25b0cd9c20ad8bab6f50'
) \gset replay_
SELECT 1 / ((:'replay_outcome'='replayed')::integer);
SELECT 1 / ((:'replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'replay_review_id'=:'review_review_id')::integer);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_projection_review_v5(
      '33000000-0000-4000-8000-000000000001'::uuid,
      'p01','authorized','system',
      'memory_v1_v5_entity_resolution_projection_preparation_20260716',
      'direct user statement with applied owner-scoped entity bindings; phase-authorized projection preparation',
      '["direct_user_statement","applied_entity_bindings","phase_authorized_projection_preparation"]'::jsonb
    );
    RAISE EXCEPTION 'cross-owner projection review unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$isolation$;
RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 1 / (((
  SELECT count(*) FROM memory.projection_review
  WHERE plan_id='33000000-0000-4000-8000-000000000001'::uuid
)=0)::integer);
SELECT 'memory_v1_projection_review_live_v5: PASS' AS result;
