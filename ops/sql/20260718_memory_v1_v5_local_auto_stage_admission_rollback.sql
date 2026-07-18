BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $guard$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local auto-stage admission rollback requires sage';
  END IF;
  IF to_regclass('memory.v5_local_packet_stage_admission') IS NOT NULL
     AND EXISTS (SELECT 1 FROM memory.v5_local_packet_stage_admission) THEN
    RAISE EXCEPTION 'local auto-stage admission rollback requires an empty table';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.register_owner_v5_local_auto_stage_v1(
  uuid,uuid,uuid,text,text,text,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_local_auto_stage_v1(integer);
DROP TABLE IF EXISTS memory.v5_local_packet_stage_admission;
ALTER TABLE memory.v5_local_packet_review_artifact
  DROP CONSTRAINT IF EXISTS v5_local_packet_review_artifact_owner_artifact_key;

COMMIT;
