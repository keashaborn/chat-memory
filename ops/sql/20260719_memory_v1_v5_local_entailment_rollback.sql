BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $guard$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local entailment rollback requires sage';
  END IF;
  IF to_regclass('memory.v5_local_entailment_assessment') IS NOT NULL
     AND EXISTS (SELECT 1 FROM memory.v5_local_entailment_assessment) THEN
    RAISE EXCEPTION 'refusing rollback with local entailment assessments';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.register_owner_v5_local_entailment_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
  memory.observation_entailment_decision_v5,text,jsonb,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_local_entailment_v1(integer);
DROP TABLE IF EXISTS memory.v5_local_entailment_assessment;

DROP POLICY IF EXISTS local_entailment_read
  ON memory.v5_local_packet_stage_admission;
DROP POLICY IF EXISTS local_entailment_read ON memory.relational_stage_batch;
DROP POLICY IF EXISTS local_entailment_read ON memory.observation;
DROP POLICY IF EXISTS local_entailment_read ON memory.entity_mention;
DROP POLICY IF EXISTS local_entailment_read
  ON memory.observation_entity_binding;
DROP POLICY IF EXISTS local_entailment_read ON memory.evidence;
DROP POLICY IF EXISTS local_entailment_read
  ON memory.observation_entailment_v5;
DROP POLICY IF EXISTS local_entailment_read
  ON memory.relational_operation_request;

DO $role$
BEGIN
  IF to_regrole('memory_v5_local_entailment_maintainer') IS NOT NULL THEN
    REVOKE SELECT ON
      memory.v5_local_packet_stage_admission,
      memory.relational_stage_batch,
      memory.observation,
      memory.entity_mention,
      memory.observation_entity_binding,
      memory.evidence,
      memory.observation_entailment_v5,
      memory.relational_operation_request
    FROM memory_v5_local_entailment_maintainer;
    REVOKE USAGE ON TYPE memory.observation_entailment_decision_v5
      FROM memory_v5_local_entailment_maintainer;
    REVOKE USAGE ON SCHEMA memory
      FROM memory_v5_local_entailment_maintainer;
    REVOKE EXECUTE ON FUNCTION memory.preflight_observation_entailment_v5(
      uuid,memory.observation_entailment_decision_v5,text,jsonb,text,text
    ) FROM memory_v5_local_entailment_maintainer;
    REVOKE EXECUTE ON FUNCTION memory.record_observation_entailment_v5(
      uuid,uuid,memory.observation_entailment_decision_v5,
      text,jsonb,text,text,text
    ) FROM memory_v5_local_entailment_maintainer;
    DROP ROLE memory_v5_local_entailment_maintainer;
  END IF;
END
$role$;

COMMIT;
