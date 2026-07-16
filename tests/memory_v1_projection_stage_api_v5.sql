\set ON_ERROR_STOP on

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT $packet$
{"contract_version":"memory_v1_projection_plan_v5","packet_sha256":"b8860a6714792a33ea873d4b216fc18366cbb33795e29ddea044a0c67462e9ae","predicate_registry_version":"memory_predicate_registry_v5","projection_policy_version":"memory_projection_policy_v5","projections":[{"identity":{"modality":"asserted","object_entity_id":"a7f071ff-d519-4345-890f-96557c52b109","object_kind":"entity","object_literal_sha256":null,"polarity":"affirmed","predicate":"occupation.works_as","semantic_key_sha256":"f14ab73b4b2eb5db5494878638d229367af1a170902bd92bcc6c261ea50c3082","subject_entity_id":"35029129-27bd-457b-8cb5-82dd37ba32ba"},"lane":"claim","observation_inputs":[{"observation_id":"9bf1e6b2-1840-4524-98dc-142567ebe013","observation_sha256":"8a6ebf42194c1cb1f73edb1db3e1339db5adcc8d455ff0f4c77c1ddabc29501d","stance":"supports"}],"payload":{"canonical_text":"The user works as a personal trainer.","claim_class":"direct_claim","kind":"claim","surface_policy":"direct_or_relevant"},"projection_ref":"p01","relations":[],"review":{"authorization_required":true,"reason_codes":["initial_live_projection_requires_review"],"state":"manual_review_required"},"target":{"action":"create","aggregate_id":null,"expected_revision_number":null,"reason_codes":[]},"temporal_policy":{"canonical_source":"memory.observation_temporal","materialization":"link_only","source_observation_id":null}}],"projector":"memory_v1_deterministic_projection_v5","projector_version":"occupation_claim_v1"}
$packet$ AS packet_text \gset

SELECT * FROM memory.stage_projection_plan_v5(
  '33000000-0000-4000-8000-000000000001'::uuid,
  :'packet_text',
  'f17b7c235a2f398bdf5744f43e9e4da153ed75c55a954fb4a0cbc1c41b537138'
) \gset stage_
SELECT 1 / ((:'stage_outcome'='applied')::integer);
SELECT 1 / ((:'stage_rows_written'::integer=4)::integer);

SELECT * FROM memory.stage_projection_plan_v5(
  '33000000-0000-4000-8000-000000000001'::uuid,
  :'packet_text',
  'f17b7c235a2f398bdf5744f43e9e4da153ed75c55a954fb4a0cbc1c41b537138'
) \gset replay_
SELECT 1 / ((:'replay_outcome'='replayed')::integer);
SELECT 1 / ((:'replay_rows_written'::integer=0)::integer);

RESET SESSION AUTHORIZATION;
SELECT 1 / (((
  SELECT count(*) FROM memory.projection_plan
   WHERE plan_id='33000000-0000-4000-8000-000000000001'::uuid
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.projection_plan_item
   WHERE plan_id='33000000-0000-4000-8000-000000000001'::uuid
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.projection_claim_payload
   WHERE plan_id='33000000-0000-4000-8000-000000000001'::uuid
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.projection_plan_observation
   WHERE plan_id='33000000-0000-4000-8000-000000000001'::uuid
)=1)::integer);
ROLLBACK;

SELECT 1 / (((
  SELECT count(*) FROM memory.projection_plan
   WHERE plan_id='33000000-0000-4000-8000-000000000001'::uuid
)=0)::integer);
SELECT 'memory_v1_projection_stage_api_v5: PASS' AS result;
