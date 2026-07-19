BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $guard$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local auto-resolution rollback requires sage';
  END IF;
  IF to_regclass('memory.v5_local_auto_resolution_admission') IS NOT NULL
     AND EXISTS (SELECT 1 FROM memory.v5_local_auto_resolution_admission) THEN
    RAISE EXCEPTION 'refusing to drop nonempty local auto-resolution admission';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.register_owner_v5_local_auto_resolution_v1(
  uuid,uuid,uuid,uuid,uuid,uuid,text,text,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_local_auto_resolution_v1(integer);
DROP TABLE IF EXISTS memory.v5_local_auto_resolution_admission;
DROP POLICY IF EXISTS local_auto_resolution_read
  ON memory.relational_operation_request;
DROP POLICY IF EXISTS local_auto_resolution_read
  ON memory.entity_resolution_plan;
DROP POLICY IF EXISTS local_auto_resolution_read
  ON memory.entity_mention;
DROP POLICY IF EXISTS local_auto_resolution_read
  ON memory.entity_resolution_review;
DROP POLICY IF EXISTS local_auto_resolution_read
  ON memory.entity_resolution_apply;
ALTER TABLE memory.v5_local_packet_stage_admission
  DROP CONSTRAINT IF EXISTS v5_local_packet_stage_admission_owner_admission_key;

COMMIT;
