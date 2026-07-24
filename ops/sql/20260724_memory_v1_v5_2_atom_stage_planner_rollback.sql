BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'V5.2 atom stage planner rollback requires sage';
  END IF;
END
$preflight$;

DROP FUNCTION IF EXISTS memory.plan_owner_v5_2_atom_stage_v1(uuid);

COMMIT;
