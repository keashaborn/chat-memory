\set ON_ERROR_STOP on

BEGIN;

DO $preflight$
BEGIN
  IF session_user <> 'sage' THEN
    RAISE EXCEPTION 'V5 local compiler retry v2 rollback requires sage';
  END IF;
END
$preflight$;

DROP FUNCTION IF EXISTS memory.requeue_owner_local_compiler_failure_v2(
  uuid,uuid,text,uuid,uuid,integer,text,text,text
);

COMMIT;
