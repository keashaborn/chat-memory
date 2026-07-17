\set ON_ERROR_STOP on

SELECT 1 / ((memory.v5_deferred_support_state_eligible(
  '[{
    "observation_id":"9bf1e6b2-1840-4524-98dc-142567ebe013",
    "observation_sha256":"8a6ebf42194c1cb1f73edb1db3e1339db5adcc8d455ff0f4c77c1ddabc29501d",
    "evidence_id":"fca9e5dc-83c2-4456-8db8-1fe6102eb74d",
    "evidence_content_sha256":"d41de5228017def6a31b3286cfb4033b8c26a251e73b3c16af67990bfb276de6",
    "stance":"supports",
    "decision_id":"bd374be5-e908-43f5-8c5d-76a8d041cd75",
    "decision":"deferred",
    "reason_code":"source_contradicts_predicate",
    "decision_authorization_manifest_sha256":"09e5a9879471aa8de4ef943dac0664bf242d12a747999a71b0c2665aee9e2f97"
  }]'::jsonb
))::integer);
SELECT 1 / ((NOT memory.v5_deferred_support_state_eligible(
  '[{
    "observation_id":"93024235-89a8-49d5-88fa-7e4a143b68f3",
    "observation_sha256":"28b4cb5db9082ca6b718cb7588a1740b1d9dbb6ddd5486d90758facfe00fb675",
    "evidence_id":"36e92633-08fd-441f-8d1d-27f16c2a3479",
    "evidence_content_sha256":"bd2e59f74888b232822500c0ac4fae12a96f85b1121da4507a0f5efc9b4bb9ce",
    "stance":"supports",
    "decision_id":"e4999e56-eccc-48ae-a3aa-8374e2689b6e",
    "decision":"accepted",
    "reason_code":"predicate_entailment_v5_1_accepted",
    "decision_authorization_manifest_sha256":"3eb618da143b84a30ef56dc9c2a809b893caab89e60e4f1086deb9199e315089"
  }]'::jsonb
))::integer);
SELECT 1 / ((NOT memory.v5_deferred_support_state_eligible(
  '[{
    "observation_id":"9bf1e6b2-1840-4524-98dc-142567ebe013",
    "observation_sha256":"8a6ebf42194c1cb1f73edb1db3e1339db5adcc8d455ff0f4c77c1ddabc29501d",
    "evidence_id":"fca9e5dc-83c2-4456-8db8-1fe6102eb74d",
    "evidence_content_sha256":"d41de5228017def6a31b3286cfb4033b8c26a251e73b3c16af67990bfb276de6",
    "stance":"supports",
    "decision":null,
    "reason_code":null
  }]'::jsonb
))::integer);

BEGIN;
SET SESSION AUTHORIZATION brains_app;
DO $missing_actor$
BEGIN
  BEGIN
    PERFORM * FROM memory.scan_deferred_entailment_reconciliation_v5(25);
    RAISE EXCEPTION 'missing-actor scanner unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$missing_actor$;

SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT 1 / (((SELECT count(*)
  FROM memory.scan_deferred_entailment_reconciliation_v5(25))=0)::integer);

DO $invalid_limit$
BEGIN
  BEGIN
    PERFORM * FROM memory.scan_deferred_entailment_reconciliation_v5(0);
    RAISE EXCEPTION 'zero scanner limit unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN
    NULL;
  END;
  BEGIN
    PERFORM * FROM memory.scan_deferred_entailment_reconciliation_v5(101);
    RAISE EXCEPTION 'oversized scanner limit unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN
    NULL;
  END;
END
$invalid_limit$;

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT 1 / (((SELECT count(*)
  FROM memory.scan_deferred_entailment_reconciliation_v5(25))=0)::integer);
RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 1 / (((
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND claim_id='50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid
    AND status='retracted' AND confidence=0
)=1)::integer);
SELECT 'memory_v1_deferred_reconciliation_scanner_v5: PASS' AS result;
