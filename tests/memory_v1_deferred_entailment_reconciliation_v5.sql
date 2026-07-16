\set ON_ERROR_STOP on

BEGIN;
SET SESSION AUTHORIZATION brains_app;

DO $missing_actor$
BEGIN
  BEGIN
    PERFORM * FROM
      memory.preflight_deferred_entailment_reconciliation_v5(
        '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
        'bd374be5-e908-43f5-8c5d-76a8d041cd75'::uuid
      );
    RAISE EXCEPTION 'missing-actor reconciliation unexpectedly succeeded';
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
    PERFORM 1 FROM memory.claim_entailment_reconciliation_v5 LIMIT 1;
    RAISE EXCEPTION 'brains_app directly read reconciliation audit';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
  BEGIN
    INSERT INTO memory.claim_entailment_reconciliation_v5(
      reconciliation_id,owner_user_id,claim_id,
      observation_id,observation_stance,decision_id,
      decision_authorization_manifest_sha256,
      support_state,support_state_sha256,
      review_request_id,review_id,
      review_authorization_manifest_sha256,
      apply_request_id,apply_event_id,assessment_id,
      apply_manifest_sha256,from_status,to_status,
      prior_revision_number,resulting_revision_number,
      reconciliation_policy_version,
      reconciliation_manifest_sha256,invoked_by_session
    ) VALUES (
      gen_random_uuid(),'1240822d-ac9a-4096-95aa-e2b24d36ef50',
      '50ebf1af-b072-4bf9-badc-2df7585f12c6',
      '9bf1e6b2-1840-4524-98dc-142567ebe013','supports',
      'bd374be5-e908-43f5-8c5d-76a8d041cd75',
      repeat('a',64),'[]',repeat('b',64),
      gen_random_uuid(),gen_random_uuid(),repeat('c',64),
      gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),
      repeat('d',64),'candidate','retracted',1,2,
      'memory_v1_deferred_entailment_reconciliation_v5',
      repeat('e',64),'brains_app'
    );
    RAISE EXCEPTION 'brains_app directly wrote reconciliation audit';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$direct_table_denial$;

SELECT * FROM memory.preflight_deferred_entailment_reconciliation_v5(
  '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
  'bd374be5-e908-43f5-8c5d-76a8d041cd75'::uuid
) \gset preflight_

SELECT 1 / ((:'preflight_observation_id'
  ='9bf1e6b2-1840-4524-98dc-142567ebe013')::integer);
SELECT 1 / ((:'preflight_from_status'='candidate')::integer);
SELECT 1 / ((:'preflight_target_status'='retracted')::integer);
SELECT 1 / ((:'preflight_current_revision_number'::integer=1)::integer);
SELECT 1 / ((jsonb_array_length(:'preflight_support_state'::jsonb)=1)::integer);

SELECT * FROM memory.reconcile_deferred_entailment_claim_v5(
  '43000000-0000-4000-8000-000000000001'::uuid,
  '43000000-0000-4000-8000-000000000002'::uuid,
  '43000000-0000-4000-8000-000000000003'::uuid,
  '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
  'bd374be5-e908-43f5-8c5d-76a8d041cd75'::uuid,
  :'preflight_reconciliation_manifest_sha256'
) \gset apply_

SELECT 1 / ((:'apply_outcome'='applied')::integer);
SELECT 1 / ((:'apply_rows_written'::integer=8)::integer);
SELECT 1 / ((:'apply_resulting_revision_number'::integer=2)::integer);

SELECT * FROM memory.reconcile_deferred_entailment_claim_v5(
  '43000000-0000-4000-8000-000000000001'::uuid,
  '43000000-0000-4000-8000-000000000002'::uuid,
  '43000000-0000-4000-8000-000000000003'::uuid,
  '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
  'bd374be5-e908-43f5-8c5d-76a8d041cd75'::uuid,
  :'preflight_reconciliation_manifest_sha256'
) \gset replay_

SELECT 1 / ((:'replay_outcome'='replayed')::integer);
SELECT 1 / ((:'replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'replay_reconciliation_id'
  =:'apply_reconciliation_id')::integer);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $cross_owner$
BEGIN
  BEGIN
    PERFORM * FROM
      memory.preflight_deferred_entailment_reconciliation_v5(
        '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
        'bd374be5-e908-43f5-8c5d-76a8d041cd75'::uuid
      );
    RAISE EXCEPTION 'cross-owner reconciliation unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
SELECT 1 / (((SELECT count(*)
  FROM memory.claim_entailment_reconciliation_v5)=1)::integer);
SELECT 1 / (((SELECT count(*)
  FROM memory.claim_assessment_review_v5)=1)::integer);
SELECT 1 / (((SELECT count(*)
  FROM memory.claim_assessment_apply_v5)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.relational_operation_request
  WHERE request_id IN (
    '43000000-0000-4000-8000-000000000002'::uuid,
    '43000000-0000-4000-8000-000000000003'::uuid
  )
)=2)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
    AND status='retracted' AND confidence=0
)=1)::integer);

ROLLBACK;
RESET SESSION AUTHORIZATION;

SELECT 1 / (((SELECT count(*)
  FROM memory.claim_entailment_reconciliation_v5)=0)::integer);
SELECT 1 / (((SELECT count(*)
  FROM memory.claim_assessment_review_v5)=0)::integer);
SELECT 1 / (((SELECT count(*)
  FROM memory.claim_assessment_apply_v5)=0)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.relational_operation_request
  WHERE request_id IN (
    '43000000-0000-4000-8000-000000000002'::uuid,
    '43000000-0000-4000-8000-000000000003'::uuid
  )
)=0)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
    AND status='candidate' AND confidence=0.500
)=1)::integer);

SELECT 'memory_v1_deferred_entailment_reconciliation_v5: PASS' AS result;
