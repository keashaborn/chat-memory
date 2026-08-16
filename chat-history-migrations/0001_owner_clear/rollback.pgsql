SET ROLE sage;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $guard$
BEGIN
  IF pg_catalog.to_regclass('chat_history_private.clear_receipt') IS NOT NULL
     AND EXISTS (SELECT 1 FROM chat_history_private.clear_receipt)
  THEN
    RAISE EXCEPTION 'chat history clear receipts must be empty before rollback';
  END IF;
END;
$guard$;

REVOKE EXECUTE ON FUNCTION chat_history_private.clear_history(
  uuid, text, uuid, integer
) FROM brains_app;
REVOKE USAGE ON SCHEMA chat_history_private FROM brains_app;
DROP FUNCTION chat_history_private.clear_history(uuid, text, uuid, integer);
DROP TRIGGER clear_receipt_immutable
  ON chat_history_private.clear_receipt;
DROP FUNCTION chat_history_private.guard_receipt_immutable();
DROP TABLE chat_history_private.clear_receipt;
DROP SCHEMA chat_history_private;

RESET ROLE;
