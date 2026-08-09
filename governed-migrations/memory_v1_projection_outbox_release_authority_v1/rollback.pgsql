ALTER TABLE memory.relational_operation_request
  DROP CONSTRAINT relational_operation_request_operation_check;
ALTER TABLE memory.relational_operation_request
  ADD CONSTRAINT relational_operation_request_operation_check
  CHECK (operation = ANY (ARRAY[
    'stage_packet'::text,
    'review_resolution'::text,
    'apply_resolution'::text,
    'review_claim_assessment_v5'::text,
    'apply_claim_assessment_v5'::text,
    'record_observation_entailment_v5'::text,
    'review_claim_temporal_reconciliation_v5_2'::text,
    'apply_claim_temporal_reconciliation_v5_2'::text
  ]));
