BEGIN;

DO $guard$
DECLARE
  operation_contract text;
  expected constant text :=
    $check$CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text, 'review_claim_assessment_v5'::text, 'apply_claim_assessment_v5'::text])))$check$;
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 claim assessment rollback requires sage';
  END IF;
  IF EXISTS (SELECT 1 FROM memory.claim_assessment_apply_v5)
     OR EXISTS (SELECT 1 FROM memory.claim_assessment_review_v5) THEN
    RAISE EXCEPTION 'V5 claim assessment rollback refuses nonempty tables';
  END IF;
  SELECT pg_get_constraintdef(oid) INTO operation_contract
  FROM pg_constraint
  WHERE conrelid='memory.relational_operation_request'::regclass
    AND conname='relational_operation_request_operation_check';
  IF operation_contract IS DISTINCT FROM expected THEN
    RAISE EXCEPTION 'relational operation contract changed';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.apply_claim_assessment_v5(
  uuid,uuid,uuid,text
);
DROP FUNCTION IF EXISTS memory.preflight_claim_assessment_apply_v5(
  uuid,uuid
);
DROP FUNCTION IF EXISTS memory.review_claim_assessment_v5(
  uuid,uuid,memory.claim_assessment_action_v5,
  numeric,numeric,numeric,numeric,jsonb,text,text,text,text
);
DROP FUNCTION IF EXISTS memory.preflight_claim_assessment_review_v5(
  uuid,memory.claim_assessment_action_v5,numeric,numeric,numeric,numeric,
  jsonb,text,text,text
);
DROP FUNCTION IF EXISTS memory.claim_assessment_state_v5(uuid);
DROP TABLE memory.claim_assessment_apply_v5;
DROP TABLE memory.claim_assessment_review_v5;
DROP TYPE memory.claim_assessment_action_v5;

ALTER TABLE memory.relational_operation_request
  DROP CONSTRAINT relational_operation_request_operation_check;
ALTER TABLE memory.relational_operation_request
  ADD CONSTRAINT relational_operation_request_operation_check
  CHECK (operation IN (
    'stage_packet',
    'review_resolution',
    'apply_resolution'
  ));

COMMIT;
