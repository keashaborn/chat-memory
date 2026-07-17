\set ON_ERROR_STOP on

CREATE OR REPLACE FUNCTION pg_temp.assert_projection_packet_denied(
  packet_text text
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_projection_packet_v5(
      '33000000-0000-4000-8000-000000000001'::uuid,
      packet_text
    );
    RAISE EXCEPTION 'cross-owner projection packet unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '23514' THEN
    NULL;
  END;
END
$function$;

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

SELECT * FROM memory.preflight_projection_source_v5(
  '9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid
) \gset source_
SELECT 1 / ((:'source_predicate'='occupation.works_as')::integer);
SELECT 1 / ((
  :'source_subject_entity_id'='35029129-27bd-457b-8cb5-82dd37ba32ba'
)::integer);
SELECT 1 / ((
  :'source_object_entity_id'='a7f071ff-d519-4345-890f-96557c52b109'
)::integer);

SELECT $packet$
{"contract_version":"memory_v1_projection_plan_v5","packet_sha256":"b8860a6714792a33ea873d4b216fc18366cbb33795e29ddea044a0c67462e9ae","predicate_registry_version":"memory_predicate_registry_v5","projection_policy_version":"memory_projection_policy_v5","projections":[{"identity":{"modality":"asserted","object_entity_id":"a7f071ff-d519-4345-890f-96557c52b109","object_kind":"entity","object_literal_sha256":null,"polarity":"affirmed","predicate":"occupation.works_as","semantic_key_sha256":"f14ab73b4b2eb5db5494878638d229367af1a170902bd92bcc6c261ea50c3082","subject_entity_id":"35029129-27bd-457b-8cb5-82dd37ba32ba"},"lane":"claim","observation_inputs":[{"observation_id":"9bf1e6b2-1840-4524-98dc-142567ebe013","observation_sha256":"8a6ebf42194c1cb1f73edb1db3e1339db5adcc8d455ff0f4c77c1ddabc29501d","stance":"supports"}],"payload":{"canonical_text":"The user works as a personal trainer.","claim_class":"direct_claim","kind":"claim","surface_policy":"direct_or_relevant"},"projection_ref":"p01","relations":[],"review":{"authorization_required":true,"reason_codes":["initial_live_projection_requires_review"],"state":"manual_review_required"},"target":{"action":"create","aggregate_id":null,"expected_revision_number":null,"reason_codes":[]},"temporal_policy":{"canonical_source":"memory.observation_temporal","materialization":"link_only","source_observation_id":null}}],"projector":"memory_v1_deterministic_projection_v5","projector_version":"occupation_claim_v1"}
$packet$ AS packet_text \gset

SELECT * FROM memory.preflight_projection_packet_v5(
  '33000000-0000-4000-8000-000000000001'::uuid,
  :'packet_text'
) \gset packet_
SELECT 1 / ((length(:'packet_packet_text_sha256')=64)::integer);
SELECT 1 / ((
  :'packet_semantic_key_sha256'
    ='f14ab73b4b2eb5db5494878638d229367af1a170902bd92bcc6c261ea50c3082'
)::integer);
SELECT 1 / ((
  :'packet_projection_sha256'
    ='531e85b111a7e34736f2b741b5b0bf36676df8b552cd2d1e67376e90f6e8f1d5'
)::integer);
SELECT 1 / ((
  :'packet_packet_sha256'
    ='b8860a6714792a33ea873d4b216fc18366cbb33795e29ddea044a0c67462e9ae'
)::integer);
SELECT 1 / ((
  :'packet_owner_manifest_sha256'
    ='f17b7c235a2f398bdf5744f43e9e4da153ed75c55a954fb4a0cbc1c41b537138'
)::integer);
SELECT 1 / ((:'packet_existing_claims'::integer=0)::integer);
SELECT 1 / ((:'packet_existing_plans'::integer=0)::integer);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_projection_source_v5(
      '9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid
    );
    RAISE EXCEPTION 'cross-owner projection source unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$isolation$;
SELECT pg_temp.assert_projection_packet_denied(:'packet_text');

RESET SESSION AUTHORIZATION;
ROLLBACK;
SELECT 'memory_v1_projection_preflight_api_v5: PASS' AS result;
