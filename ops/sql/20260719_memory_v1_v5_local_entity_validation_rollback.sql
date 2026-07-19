BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $guard$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local entity validation rollback requires sage';
  END IF;
  IF to_regclass('memory.v5_local_entity_validation_assessment') IS NOT NULL
     AND EXISTS (SELECT 1 FROM memory.v5_local_entity_validation_assessment) THEN
    RAISE EXCEPTION 'refusing rollback with entity validation assessments';
  END IF;
  IF to_regclass('memory.v5_local_validated_stage_admission') IS NOT NULL
     AND EXISTS (SELECT 1 FROM memory.v5_local_validated_stage_admission) THEN
    RAISE EXCEPTION 'refusing rollback with validated stage admissions';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.register_owner_v5_local_validated_stage_v1(
  uuid,uuid,uuid,uuid,uuid,text,text
);
DROP FUNCTION IF EXISTS memory.register_owner_v5_local_entity_validation_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,text,
  text,text,text,text,jsonb,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_local_entity_validation_v1(integer);
DROP TABLE IF EXISTS memory.v5_local_validated_stage_admission;
DROP TABLE IF EXISTS memory.v5_local_entity_validation_assessment;

DO $policies$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'memory.v5_local_packet_review_artifact'::regclass,
    'memory.v5_local_packet_stage_admission'::regclass,
    'memory.evidence_extraction_packet_v5_local'::regclass,
    'memory.evidence_extraction_job'::regclass,
    'memory.evidence'::regclass,
    'memory.relational_stage_batch'::regclass,
    'memory.entity_mention'::regclass,
    'memory.entity_resolution_plan'::regclass
  ] LOOP
    EXECUTE format('DROP POLICY IF EXISTS local_entity_validation_read ON %s',target);
  END LOOP;
END
$policies$;

DO $role$
BEGIN
  IF to_regrole('memory_v5_local_entity_validation_maintainer') IS NOT NULL THEN
    REVOKE USAGE ON SCHEMA memory
      FROM memory_v5_local_entity_validation_maintainer;
    REVOKE SELECT ON memory.v5_local_packet_review_artifact,
      memory.v5_local_packet_stage_admission,
      memory.evidence_extraction_packet_v5_local,
      memory.evidence_extraction_job,memory.evidence,
      memory.relational_stage_batch,memory.entity_mention,
      memory.entity_resolution_plan
    FROM memory_v5_local_entity_validation_maintainer;
    REVOKE INSERT ON memory.v5_local_packet_stage_admission
      FROM memory_v5_local_entity_validation_maintainer;
    REVOKE EXECUTE ON FUNCTION memory.current_actor_user_id()
      FROM memory_v5_local_entity_validation_maintainer;
    REVOKE EXECUTE ON FUNCTION memory.v5_digest_text(text)
      FROM memory_v5_local_entity_validation_maintainer;
    REVOKE EXECUTE ON FUNCTION memory.v5_sha256_valid(text)
      FROM memory_v5_local_entity_validation_maintainer;
    REVOKE EXECUTE ON FUNCTION memory.v5_canonical_json_text(jsonb)
      FROM memory_v5_local_entity_validation_maintainer;
    DROP ROLE memory_v5_local_entity_validation_maintainer;
  END IF;
END
$role$;

ALTER TABLE memory.v5_local_packet_stage_admission
  DROP CONSTRAINT IF EXISTS
    v5_local_packet_stage_admission_policy_decision_check;
ALTER TABLE memory.v5_local_packet_stage_admission
  ADD CONSTRAINT v5_local_packet_stage_admission_decision_check
  CHECK (decision='auto_stage_eligible');
ALTER TABLE memory.v5_local_packet_stage_admission
  ADD CONSTRAINT v5_local_packet_stage_admission_policy_version_check
  CHECK (policy_version='memory_v1_v5_local_auto_stage_policy_v1');

COMMIT;
