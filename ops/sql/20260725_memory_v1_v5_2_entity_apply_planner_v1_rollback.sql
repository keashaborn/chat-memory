BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $rollback$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'V5.2 entity apply planner rollback requires sage';
  END IF;
END
$rollback$;

DROP FUNCTION IF EXISTS memory.plan_owner_v5_2_entity_apply_review_v1(uuid);

COMMIT;
