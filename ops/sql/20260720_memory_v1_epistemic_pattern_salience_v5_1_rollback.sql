BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
DECLARE
  relation_name text;
  row_count bigint;
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'epistemic pattern/salience V5.1 rollback requires sage';
  END IF;
  FOREACH relation_name IN ARRAY ARRAY[
    'pattern_hypothesis_v5_1',
    'pattern_hypothesis_revision_v5_1',
    'pattern_observation_link_v5_1',
    'epistemic_target_binding_v5_1',
    'epistemic_assessment_snapshot_v5_1',
    'epistemic_assessment_observation_link_v5_1',
    'salience_feature_snapshot_v5_1',
    'retrieval_outcome_signal_v5_1',
    'epistemic_operation_request_v5_1'
  ]
  LOOP
    IF to_regclass('memory.'||relation_name) IS NOT NULL THEN
      EXECUTE format('SELECT count(*) FROM memory.%I',relation_name)
        INTO row_count;
      IF row_count<>0 THEN
        RAISE EXCEPTION 'refusing non-empty V5.1 rollback: memory.% has % rows',
          relation_name,row_count;
      END IF;
    END IF;
  END LOOP;
END
$preflight$;

REVOKE EXECUTE ON FUNCTION memory.persist_epistemic_snapshot_packet_v5_1(
  uuid,jsonb
) FROM brains_app;
REVOKE EXECUTE ON FUNCTION memory.record_retrieval_outcome_signal_v5_1(
  uuid,uuid,uuid,memory.retrieval_outcome_v5_1,uuid,text
) FROM brains_app;

DROP FUNCTION memory.record_retrieval_outcome_signal_v5_1(
  uuid,uuid,uuid,memory.retrieval_outcome_v5_1,uuid,text
);
DROP FUNCTION memory.persist_epistemic_snapshot_packet_v5_1(uuid,jsonb);
DROP FUNCTION memory.epistemic_packet_sha256_v5_1(jsonb);
DROP FUNCTION memory.require_v5_epistemic_writer_context();

DO $reference_policies$
DECLARE
  relation_name text;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY[
    'entity','evidence','claim','claim_revision','observation',
    'preference_head_v5','preference_revision_v5',
    'project_knowledge_head_v5','project_knowledge_revision_v5',
    'retrieval_trace'
  ]
  LOOP
    IF to_regclass('memory.'||relation_name) IS NOT NULL THEN
      EXECUTE format(
        'DROP POLICY IF EXISTS epistemic_v5_1_reference_read ON memory.%I',
        relation_name
      );
    END IF;
  END LOOP;
END
$reference_policies$;

DROP TABLE memory.epistemic_operation_request_v5_1;
DROP TABLE memory.retrieval_outcome_signal_v5_1;
DROP TABLE memory.salience_feature_snapshot_v5_1;
DROP TABLE memory.epistemic_assessment_observation_link_v5_1;
DROP TABLE memory.epistemic_assessment_snapshot_v5_1;
DROP TABLE memory.epistemic_target_binding_v5_1;
DROP TABLE memory.pattern_observation_link_v5_1;
ALTER TABLE memory.pattern_hypothesis_v5_1
  DROP CONSTRAINT pattern_hypothesis_v5_1_current_revision_fk;
DROP TABLE memory.pattern_hypothesis_revision_v5_1;
DROP TABLE memory.pattern_hypothesis_v5_1;

DROP TYPE memory.retrieval_outcome_v5_1;
DROP TYPE memory.epistemic_observation_role_v5_1;
DROP TYPE memory.pattern_observation_role_v5_1;
DROP TYPE memory.pattern_state_v5_1;
DROP TYPE memory.pattern_kind_v5_1;
DROP TYPE memory.epistemic_assessment_state_v5_1;
DROP TYPE memory.epistemic_target_kind_v5_1;

DO $drop_role$
BEGIN
  IF to_regrole('memory_v5_epistemic_writer') IS NOT NULL THEN
    DROP OWNED BY memory_v5_epistemic_writer;
    DROP ROLE memory_v5_epistemic_writer;
  END IF;
END
$drop_role$;

COMMIT;
