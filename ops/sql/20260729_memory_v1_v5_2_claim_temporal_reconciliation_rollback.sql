BEGIN;

DO $safe$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'V5.2 claim temporal reconciliation rollback requires sage';
  END IF;
  IF to_regclass(
       'memory.claim_temporal_reconciliation_review_v5_2'
     ) IS NOT NULL
     AND EXISTS (
       SELECT 1
       FROM memory.claim_temporal_reconciliation_review_v5_2
     ) THEN
    RAISE EXCEPTION 'review rows exist; restore the production backup';
  END IF;
  IF to_regclass(
       'memory.claim_temporal_reconciliation_apply_v5_2'
     ) IS NOT NULL
     AND EXISTS (
       SELECT 1
       FROM memory.claim_temporal_reconciliation_apply_v5_2
     ) THEN
    RAISE EXCEPTION 'apply rows exist; restore the production backup';
  END IF;
END
$safe$;

DROP FUNCTION IF EXISTS
  memory.apply_claim_temporal_reconciliation_v5_2(
    uuid,uuid,uuid,text
  );
DROP FUNCTION IF EXISTS
  memory.preflight_claim_temporal_reconciliation_apply_v5_2(uuid,uuid);
DROP FUNCTION IF EXISTS
  memory.review_claim_temporal_reconciliation_v5_2(
    uuid,uuid,uuid,jsonb,text,text,text,text
  );
DROP FUNCTION IF EXISTS
  memory.preflight_claim_temporal_reconciliation_v5_2(uuid,uuid);
DROP TABLE IF EXISTS memory.claim_temporal_reconciliation_apply_v5_2;
DROP TABLE IF EXISTS memory.claim_temporal_reconciliation_review_v5_2;

DO $operation_contract$
DECLARE
  definition text;
  installed constant text :=
    $new$CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text, 'review_claim_assessment_v5'::text, 'apply_claim_assessment_v5'::text, 'record_observation_entailment_v5'::text, 'review_claim_temporal_reconciliation_v5_2'::text, 'apply_claim_temporal_reconciliation_v5_2'::text])))$new$;
  predecessor constant text :=
    $old$CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text, 'review_claim_assessment_v5'::text, 'apply_claim_assessment_v5'::text, 'record_observation_entailment_v5'::text])))$old$;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO definition
  FROM pg_constraint
  WHERE conrelid='memory.relational_operation_request'::regclass
    AND conname='relational_operation_request_operation_check';
  IF definition=predecessor THEN
    NULL;
  ELSIF definition=installed THEN
    ALTER TABLE memory.relational_operation_request
      DROP CONSTRAINT relational_operation_request_operation_check;
    ALTER TABLE memory.relational_operation_request
      ADD CONSTRAINT relational_operation_request_operation_check
      CHECK (operation IN (
        'stage_packet',
        'review_resolution',
        'apply_resolution',
        'review_claim_assessment_v5',
        'apply_claim_assessment_v5',
        'record_observation_entailment_v5'
      ));
  ELSE
    RAISE EXCEPTION 'relational operation contract changed';
  END IF;
END
$operation_contract$;

COMMIT;
