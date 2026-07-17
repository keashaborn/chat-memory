\set ON_ERROR_STOP on

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT * FROM memory.apply_projection_v5(
  '34000000-0000-4000-8000-000000000001'::uuid,
  '33000000-0000-4000-8000-000000000001'::uuid,
  'p01',
  '79f6d3fe-2a8f-486a-8681-e3a66420385d'::uuid,
  '7ca9fb9157392e38ed5e8d8f136c970bfb25226b0208d0f81b30f42c5c0b2486'
) \gset apply_
SELECT 1 / ((:'apply_outcome'='applied')::integer);
SELECT 1 / ((:'apply_lane'='claim')::integer);
SELECT 1 / ((:'apply_revision_number'::integer=1)::integer);
SELECT 1 / ((:'apply_rows_written'::integer=5)::integer);

SELECT * FROM memory.apply_projection_v5(
  '34000000-0000-4000-8000-000000000001'::uuid,
  '33000000-0000-4000-8000-000000000001'::uuid,
  'p01',
  '79f6d3fe-2a8f-486a-8681-e3a66420385d'::uuid,
  '7ca9fb9157392e38ed5e8d8f136c970bfb25226b0208d0f81b30f42c5c0b2486'
) \gset replay_
SELECT 1 / ((:'replay_outcome'='replayed')::integer);
SELECT 1 / ((:'replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'replay_aggregate_id'=:'apply_aggregate_id')::integer);
SELECT 1 / ((:'replay_revision_id'=:'apply_revision_id')::integer);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_projection_apply_v5(
      '33000000-0000-4000-8000-000000000001'::uuid,
      'p01',
      '79f6d3fe-2a8f-486a-8681-e3a66420385d'::uuid
    );
    RAISE EXCEPTION 'cross-owner projection apply unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$isolation$;

RESET SESSION AUTHORIZATION;
SELECT 1 / (((
  SELECT count(*) FROM memory.claim
   WHERE claim_id=:'apply_aggregate_id'::uuid
     AND canonical_key='v5:f14ab73b4b2eb5db5494878638d229367af1a170902bd92bcc6c261ea50c3082'
     AND status='candidate'
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.claim_revision
   WHERE claim_id=:'apply_aggregate_id'::uuid
     AND revision_id=:'apply_revision_id'::uuid
     AND revision_number=1
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.claim_observation
   WHERE claim_id=:'apply_aggregate_id'::uuid
     AND observation_id='9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.projection_apply_event
   WHERE event_id=:'apply_apply_event_id'::uuid
     AND request_id='34000000-0000-4000-8000-000000000001'::uuid
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.projection_dispatch_v5
   WHERE apply_event_id=:'apply_apply_event_id'::uuid
)=1)::integer);
ROLLBACK;

SELECT 1 / (((
  SELECT count(*) FROM memory.claim
   WHERE canonical_key='v5:f14ab73b4b2eb5db5494878638d229367af1a170902bd92bcc6c261ea50c3082'
)=0)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.projection_apply_event
   WHERE request_id='34000000-0000-4000-8000-000000000001'::uuid
)=0)::integer);
SELECT 'memory_v1_projection_materialization_live_v5: PASS' AS result;
