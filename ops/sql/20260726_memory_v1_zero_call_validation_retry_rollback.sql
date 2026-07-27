\set ON_ERROR_STOP on

BEGIN;

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'zero-call validation retry rollback requires sage';
  END IF;
END
$preflight$;

DROP FUNCTION IF EXISTS
memory.requeue_owner_local_zero_call_validation_failure_v1(
  uuid,uuid,text,uuid,uuid,integer,text
);

COMMIT;
