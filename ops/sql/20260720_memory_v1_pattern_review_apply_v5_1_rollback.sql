BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $guard$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'pattern review/apply V5.1 rollback requires sage';
  END IF;
  IF (to_regclass('memory.pattern_review_v5_1') IS NOT NULL
      AND EXISTS (SELECT 1 FROM memory.pattern_review_v5_1 LIMIT 1))
     OR (to_regclass('memory.pattern_review_observation_v5_1') IS NOT NULL
      AND EXISTS (SELECT 1 FROM memory.pattern_review_observation_v5_1 LIMIT 1))
     OR (to_regclass('memory.pattern_apply_event_v5_1') IS NOT NULL
      AND EXISTS (SELECT 1 FROM memory.pattern_apply_event_v5_1 LIMIT 1))
     OR (to_regclass('memory.pattern_operation_request_v5_1') IS NOT NULL
      AND EXISTS (SELECT 1 FROM memory.pattern_operation_request_v5_1 LIMIT 1))
     OR EXISTS (SELECT 1 FROM memory.pattern_hypothesis_v5_1 LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.pattern_hypothesis_revision_v5_1 LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.pattern_observation_link_v5_1 LIMIT 1) THEN
    RAISE EXCEPTION 'pattern review/apply rollback requires empty pattern state';
  END IF;
END
$guard$;

REVOKE EXECUTE ON FUNCTION memory.apply_pattern_review_v5_1(uuid,uuid,text)
  FROM brains_app;
REVOKE EXECUTE ON FUNCTION memory.preflight_pattern_apply_v5_1(uuid)
  FROM brains_app;
REVOKE EXECUTE ON FUNCTION memory.review_pattern_v5_1(
  uuid,jsonb,memory.pattern_review_decision_v5_1,text,text,jsonb,text
) FROM brains_app;
REVOKE EXECUTE ON FUNCTION memory.preflight_pattern_review_v5_1(
  jsonb,memory.pattern_review_decision_v5_1,text,text,jsonb
) FROM brains_app;

DROP FUNCTION IF EXISTS memory.apply_pattern_review_v5_1(uuid,uuid,text);
DROP FUNCTION IF EXISTS memory.preflight_pattern_apply_v5_1(uuid);
DROP FUNCTION IF EXISTS memory.review_pattern_v5_1(
  uuid,jsonb,memory.pattern_review_decision_v5_1,text,text,jsonb,text
);
DROP FUNCTION IF EXISTS memory.preflight_pattern_review_v5_1(
  jsonb,memory.pattern_review_decision_v5_1,text,text,jsonb
);
DROP TRIGGER IF EXISTS pattern_hypothesis_v5_1_controlled_update
  ON memory.pattern_hypothesis_v5_1;
DROP FUNCTION IF EXISTS memory.guard_pattern_head_update_v5_1();
DROP FUNCTION IF EXISTS memory.pattern_head_state_v5_1(text);
DROP FUNCTION IF EXISTS memory.pattern_proposal_shape_valid_v5_1(jsonb);
DROP FUNCTION IF EXISTS memory.pattern_proposal_sha256_v5_1(jsonb);

REVOKE INSERT,UPDATE ON memory.pattern_hypothesis_v5_1
  FROM memory_v5_epistemic_writer;
REVOKE INSERT ON memory.pattern_hypothesis_revision_v5_1,
  memory.pattern_observation_link_v5_1
  FROM memory_v5_epistemic_writer;
REVOKE SELECT ON memory.observation_entity_binding
  FROM memory_v5_epistemic_writer;
DROP POLICY IF EXISTS pattern_v5_1_reference_read
  ON memory.observation_entity_binding;

DROP TABLE IF EXISTS memory.pattern_apply_event_v5_1;
DROP TABLE IF EXISTS memory.pattern_review_observation_v5_1;
DROP TABLE IF EXISTS memory.pattern_review_v5_1;
DROP TABLE IF EXISTS memory.pattern_operation_request_v5_1;
DROP TYPE IF EXISTS memory.pattern_review_decision_v5_1;

COMMIT;
