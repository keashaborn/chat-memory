BEGIN;

DO $guard$
DECLARE
  operation_contract text;
  expected constant text :=
    $check$CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text, 'review_claim_assessment_v5'::text, 'apply_claim_assessment_v5'::text, 'record_observation_entailment_v5'::text])))$check$;
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5.1 observation entailment rollback requires sage';
  END IF;
  IF EXISTS (SELECT 1 FROM memory.observation_entailment_v5)
     OR EXISTS (
       SELECT 1 FROM memory.relational_operation_request
       WHERE operation='record_observation_entailment_v5'
     ) THEN
    RAISE EXCEPTION 'V5.1 observation entailment rollback refuses live rows';
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

DROP TRIGGER IF EXISTS projection_plan_observation_entailment_guard
  ON memory.projection_plan_observation;
DROP FUNCTION memory.guard_projection_observation_entailment_v5();
DROP FUNCTION memory.observation_entailment_allows_projection_v5(uuid,text);
DROP FUNCTION memory.record_observation_entailment_v5(
  uuid,uuid,memory.observation_entailment_decision_v5,
  text,jsonb,text,text,text
);
DROP FUNCTION memory.preflight_observation_entailment_v5(
  uuid,memory.observation_entailment_decision_v5,text,jsonb,text,text
);
DROP TABLE memory.observation_entailment_v5;
DROP FUNCTION memory.v5_source_spans_cover(jsonb,jsonb);
DROP FUNCTION memory.v5_source_spans_match_text(jsonb,text);
DROP TYPE memory.observation_entailment_decision_v5;

ALTER TABLE memory.relational_operation_request
  DROP CONSTRAINT relational_operation_request_operation_check;
ALTER TABLE memory.relational_operation_request
  ADD CONSTRAINT relational_operation_request_operation_check
  CHECK (operation IN (
    'stage_packet',
    'review_resolution',
    'apply_resolution',
    'review_claim_assessment_v5',
    'apply_claim_assessment_v5'
  ));

COMMIT;
