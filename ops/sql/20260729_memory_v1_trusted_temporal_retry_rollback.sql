\set ON_ERROR_STOP on

BEGIN;

DROP FUNCTION IF EXISTS
memory.requeue_owner_trusted_temporal_failure_v1(
  uuid,uuid,text,uuid,uuid,integer,text,text
);

COMMIT;
