BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $guard$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local claim projection rollback requires sage';
  END IF;
  IF to_regclass('memory.v5_local_claim_projection_admission') IS NOT NULL
     AND EXISTS (SELECT 1 FROM memory.v5_local_claim_projection_admission) THEN
    RAISE EXCEPTION 'refusing rollback with claim projection admissions';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.register_owner_v5_local_claim_projection_v1(
  uuid,uuid,uuid,uuid,text,text,text,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_local_claim_projection_v1(integer);
DROP TABLE IF EXISTS memory.v5_local_claim_projection_admission;
ALTER TABLE memory.v5_local_entailment_assessment
  DROP CONSTRAINT IF EXISTS
    v5_local_entailment_assessment_owner_assessment_key;

DROP POLICY IF EXISTS local_projection_read
  ON memory.v5_local_entailment_assessment;
DROP POLICY IF EXISTS local_projection_read ON memory.observation;
DROP POLICY IF EXISTS local_projection_read ON memory.projection_plan;

DO $role$
BEGIN
  IF to_regrole('memory_v5_local_projection_maintainer') IS NOT NULL THEN
    REVOKE USAGE ON SCHEMA memory
      FROM memory_v5_local_projection_maintainer;
    REVOKE SELECT ON memory.v5_local_entailment_assessment,
      memory.observation,memory.projection_plan
    FROM memory_v5_local_projection_maintainer;
    REVOKE EXECUTE ON FUNCTION memory.current_actor_user_id()
      FROM memory_v5_local_projection_maintainer;
    REVOKE EXECUTE ON FUNCTION memory.v5_sha256_valid(text)
      FROM memory_v5_local_projection_maintainer;
    REVOKE EXECUTE ON FUNCTION memory.stage_claim_projection_plan_v5_1(
      uuid,text,text
    ) FROM memory_v5_local_projection_maintainer;
    DROP ROLE memory_v5_local_projection_maintainer;
  END IF;
END
$role$;

COMMIT;
