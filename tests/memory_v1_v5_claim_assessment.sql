\set ON_ERROR_STOP on

BEGIN;
SET SESSION AUTHORIZATION brains_app;

DO $missing_actor$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_claim_assessment_review_v5(
      '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
      'retract'::memory.claim_assessment_action_v5,
      0.000,1.000,0.000,1.000,
      '["explicit_source_negation","projection_semantic_overreach","replacement_predicate_unavailable"]'::jsonb,
      'The projected occupation claim overstates the source.',
      'system','memory_v1_v5_claim_assessment_test'
    );
    RAISE EXCEPTION 'missing-actor assessment preflight unexpectedly succeeded';
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
    PERFORM 1 FROM memory.claim_assessment_review_v5 LIMIT 1;
    RAISE EXCEPTION 'brains_app directly read V5 assessment reviews';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
  BEGIN
    INSERT INTO memory.claim_assessment_apply_v5(
      event_id,owner_user_id,request_id,claim_id,review_id,assessment_id,
      from_status,to_status,prior_revision_number,resulting_revision_number,
      apply_manifest_sha256,invoked_by_session
    ) VALUES (
      gen_random_uuid(),'1240822d-ac9a-4096-95aa-e2b24d36ef50',
      gen_random_uuid(),'50ebf1af-b072-4bf9-badc-2df7585f12c6',
      gen_random_uuid(),gen_random_uuid(),'candidate','retracted',1,2,
      repeat('a',64),'brains_app'
    );
    RAISE EXCEPTION 'brains_app directly wrote a V5 assessment event';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$direct_table_denial$;

SELECT * FROM memory.preflight_claim_assessment_review_v5(
  '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
  'retract'::memory.claim_assessment_action_v5,
  0.000,1.000,0.000,1.000,
  '["explicit_source_negation","projection_semantic_overreach","replacement_predicate_unavailable"]'::jsonb,
  'The source explicitly says personal training is not done for a living; the occupation projection overstates it.',
  'system','memory_v1_v5_claim_assessment_test'
) \gset review_preflight_

SELECT 1 / ((:'review_preflight_from_status'='candidate')::integer);
SELECT 1 / ((:'review_preflight_target_status'='retracted')::integer);
SELECT 1 / ((:'review_preflight_current_revision_number'::integer=1)::integer);
SELECT 1 / ((length(:'review_preflight_claim_state_sha256')=64)::integer);
SELECT 1 / ((length(:'review_preflight_evidence_manifest_sha256')=64)::integer);

SELECT * FROM memory.review_claim_assessment_v5(
  '41000000-0000-4000-8000-000000000001'::uuid,
  '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
  'retract'::memory.claim_assessment_action_v5,
  0.000,1.000,0.000,1.000,
  '["explicit_source_negation","projection_semantic_overreach","replacement_predicate_unavailable"]'::jsonb,
  'The source explicitly says personal training is not done for a living; the occupation projection overstates it.',
  'system','memory_v1_v5_claim_assessment_test',
  :'review_preflight_authorization_manifest_sha256'
) \gset review_
SELECT 1 / ((:'review_outcome'='applied')::integer);

SELECT * FROM memory.review_claim_assessment_v5(
  '41000000-0000-4000-8000-000000000001'::uuid,
  '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
  'retract'::memory.claim_assessment_action_v5,
  0.000,1.000,0.000,1.000,
  '["explicit_source_negation","projection_semantic_overreach","replacement_predicate_unavailable"]'::jsonb,
  'The source explicitly says personal training is not done for a living; the occupation projection overstates it.',
  'system','memory_v1_v5_claim_assessment_test',
  :'review_preflight_authorization_manifest_sha256'
) \gset review_replay_
SELECT 1 / ((:'review_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'review_replay_review_id'=:'review_review_id')::integer);

SELECT * FROM memory.preflight_claim_assessment_apply_v5(
  '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
  :'review_review_id'::uuid
) \gset apply_preflight_
SELECT 1 / ((:'apply_preflight_from_status'='candidate')::integer);
SELECT 1 / ((:'apply_preflight_target_status'='retracted')::integer);
SELECT 1 / ((:'apply_preflight_prior_revision_number'::integer=1)::integer);

SELECT * FROM memory.apply_claim_assessment_v5(
  '42000000-0000-4000-8000-000000000001'::uuid,
  '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
  :'review_review_id'::uuid,
  :'apply_preflight_apply_manifest_sha256'
) \gset apply_
SELECT 1 / ((:'apply_outcome'='applied')::integer);
SELECT 1 / ((:'apply_resulting_revision_number'::integer=2)::integer);
SELECT 1 / ((:'apply_rows_written'::integer=5)::integer);

SELECT * FROM memory.apply_claim_assessment_v5(
  '42000000-0000-4000-8000-000000000001'::uuid,
  '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
  :'review_review_id'::uuid,
  :'apply_preflight_apply_manifest_sha256'
) \gset apply_replay_
SELECT 1 / ((:'apply_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'apply_replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'apply_replay_event_id'=:'apply_event_id')::integer);
SELECT 1 / ((:'apply_replay_assessment_id'=:'apply_assessment_id')::integer);

SELECT 1 / (((
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
    AND status='retracted' AND confidence=0.000
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.claim_assessment
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND assessment_id=:'apply_assessment_id'::uuid
    AND status='retracted' AND opposition_score=1.000
    AND method='memory_v1_claim_assessment_v5'
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.claim_revision
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
    AND revision_number=2
    AND snapshot->>'status'='retracted'
)=1)::integer);
SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_claim_assessment_review_v5(
      '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
      'retract'::memory.claim_assessment_action_v5,
      0.000,1.000,0.000,1.000,
      '["cross_owner_test"]'::jsonb,
      'Cross-owner claim must not be visible.',
      'system','memory_v1_v5_claim_assessment_test'
    );
    RAISE EXCEPTION 'cross-owner assessment unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$isolation$;

RESET SESSION AUTHORIZATION;
SELECT 1 / (((
  SELECT count(*) FROM memory.claim_assessment_apply_v5
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND event_id=:'apply_event_id'::uuid
    AND from_status='candidate' AND to_status='retracted'
)=1)::integer);

ROLLBACK;
RESET SESSION AUTHORIZATION;

SELECT 1 / (((
  SELECT count(*) FROM memory.claim
  WHERE claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
    AND status='candidate'
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.claim_revision
  WHERE claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
)=1)::integer);
SELECT 1 / (((SELECT count(*) FROM memory.claim_assessment_review_v5)=0)::integer);
SELECT 1 / (((SELECT count(*) FROM memory.claim_assessment_apply_v5)=0)::integer);

SELECT 'memory_v1_v5_claim_assessment: PASS' AS result;
