BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'authenticated owner registry rollback requires sage';
  END IF;
END
$preflight$;

DROP FUNCTION IF EXISTS
  memory.list_recent_authenticated_owners_v1(interval,integer);
DROP FUNCTION IF EXISTS
  memory.register_authenticated_owner_v1(uuid,text,text);
DROP TABLE IF EXISTS memory.authenticated_owner_registry_v1;

COMMIT;
