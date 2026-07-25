BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $rollback$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'V5.2 atom stage planner V2 rollback requires sage';
  END IF;
END
$rollback$;

DROP FUNCTION IF EXISTS memory.plan_owner_v5_2_entity_resolution_review_v1(uuid);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_2_atom_stage_v2(uuid);

COMMIT;
