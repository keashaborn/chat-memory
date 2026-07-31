BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '180s';

DO $preflight$
BEGIN
  IF session_user <> 'sage' THEN
    RAISE EXCEPTION 'AI Operations inbox rollback requires sage'
      USING ERRCODE = '42501';
  END IF;
END
$preflight$;

REVOKE ALL ON SCHEMA ai_operations FROM brains_app;
REVOKE ALL ON ALL TABLES IN SCHEMA ai_operations FROM brains_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ai_operations FROM brains_app;
DROP SCHEMA ai_operations CASCADE;

DO $role$
BEGIN
  IF to_regrole('ai_operations_store_v1') IS NOT NULL THEN
    REVOKE EXECUTE ON FUNCTION public.digest(bytea,text)
      FROM ai_operations_store_v1;
    DROP ROLE ai_operations_store_v1;
  END IF;
END
$role$;

COMMIT;
