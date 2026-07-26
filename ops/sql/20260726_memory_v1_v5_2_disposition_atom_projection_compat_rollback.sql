\set ON_ERROR_STOP on
BEGIN;

DO $rollback$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION
      'V5.2 disposition compatibility rollback requires sage'
      USING ERRCODE='42501';
  END IF;
  IF to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regprocedure(
       'memory.guard_disposed_evidence_from_stage_v1()'
     ) IS NULL THEN
    RAISE EXCEPTION
      'V5.2 disposition compatibility rollback prerequisites are absent'
      USING ERRCODE='55000';
  END IF;
END
$rollback$;

REVOKE EXECUTE ON FUNCTION
  memory.v5_2_atom_stage_projection_authorized_v1(uuid,uuid,jsonb,text)
  FROM memory_v5_local_disposition_maintainer;

DROP TRIGGER IF EXISTS v5_local_disposition_stage_guard
  ON memory.relational_stage_batch;
CREATE TRIGGER v5_local_disposition_stage_guard
BEFORE INSERT ON memory.relational_stage_batch
FOR EACH ROW
EXECUTE FUNCTION memory.guard_disposed_evidence_from_stage_v1();

DROP FUNCTION IF EXISTS memory.guard_disposed_evidence_from_stage_v2();

COMMIT;
