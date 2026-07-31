BEGIN;

REVOKE lifeswitch_chat_account_writer_v1 FROM brains_app;

DROP FUNCTION IF EXISTS lifeswitch_chat.write_account_timezone_setting_v1(
  uuid,text,bigint,text
);
DROP FUNCTION IF EXISTS lifeswitch_chat.read_account_timezone_setting_v1(uuid);

DROP TRIGGER IF EXISTS account_timezone_history_immutable_v1
  ON lifeswitch_chat.account_timezone_history_v1;
DROP FUNCTION IF EXISTS
  lifeswitch_chat.reject_account_timezone_history_mutation_v1();
DROP TABLE IF EXISTS lifeswitch_chat.account_timezone_history_v1;

DROP POLICY IF EXISTS account_timezone_owner_write_v1
  ON lifeswitch_chat.account_timezone_v1;
ALTER TABLE lifeswitch_chat.account_timezone_v1 NO FORCE ROW LEVEL SECURITY;

DROP OWNED BY lifeswitch_chat_account_writer_v1;
DROP ROLE IF EXISTS lifeswitch_chat_account_writer_v1;

COMMIT;
