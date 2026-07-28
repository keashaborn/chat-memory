BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'owner memory workbench diagnostics rollback requires sage';
  END IF;
END
$preflight$;

DROP FUNCTION IF EXISTS memory.record_owner_memory_workbench_feedback_v2(
  uuid,uuid,text,text,text,text
);
DROP FUNCTION IF EXISTS memory.list_owner_memory_workbench_v2(
  text,integer,timestamptz,uuid
);

-- The nullable diagnostic column and widened policy-version constraint are
-- intentionally retained. Removing either after v2 feedback exists would
-- destroy append-only diagnostic history. The v1 API remains compatible.

COMMIT;
