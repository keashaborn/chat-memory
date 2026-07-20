BEGIN;

REVOKE EXECUTE ON FUNCTION
  memory.load_pattern_salience_shadow_inputs_v5_1(integer,integer)
  FROM brains_app;
DROP FUNCTION IF EXISTS
  memory.load_pattern_salience_shadow_inputs_v5_1(integer,integer);

DROP POLICY IF EXISTS epistemic_v5_1_reference_read
  ON memory.observation_temporal;
DROP POLICY IF EXISTS epistemic_v5_1_reference_read
  ON memory.claim_observation;
DROP POLICY IF EXISTS epistemic_v5_1_reference_read
  ON memory.preference_revision_observation;
DROP POLICY IF EXISTS epistemic_v5_1_reference_read
  ON memory.project_knowledge_revision_observation;
DROP POLICY IF EXISTS epistemic_v5_1_reference_read
  ON memory.retrieval_outcome_signal_v5_1;

REVOKE SELECT ON memory.observation_temporal,
  memory.claim_observation,
  memory.preference_revision_observation,
  memory.project_knowledge_revision_observation,
  memory.retrieval_outcome_signal_v5_1
FROM memory_v5_epistemic_writer;

COMMIT;
