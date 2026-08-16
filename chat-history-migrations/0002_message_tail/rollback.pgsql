SET ROLE sage;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE chat_history_private.message_tail_receipt
  NO FORCE ROW LEVEL SECURITY;

DO $guard$
BEGIN
  IF pg_catalog.to_regclass(
       'chat_history_private.message_tail_receipt'
     ) IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM chat_history_private.message_tail_receipt
     )
  THEN
    RAISE EXCEPTION 'message tail receipts must be empty before rollback';
  END IF;
END;
$guard$;

REVOKE EXECUTE ON FUNCTION chat_history_private.clear_message_tail(uuid, uuid)
  FROM brains_app;
DROP FUNCTION chat_history_private.clear_message_tail(uuid, uuid);
DROP TRIGGER message_tail_receipt_immutable
  ON chat_history_private.message_tail_receipt;
DROP TABLE chat_history_private.message_tail_receipt;

RESET ROLE;
