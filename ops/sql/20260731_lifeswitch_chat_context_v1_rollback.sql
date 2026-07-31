BEGIN;

DROP TRIGGER IF EXISTS final_answer_lifeswitch_binding_immutable_v1
  ON lifeswitch_chat.final_answer_lifeswitch_binding_v1;
DROP FUNCTION IF EXISTS lifeswitch_chat.reject_binding_mutation_v1();
DROP TABLE IF EXISTS lifeswitch_chat.final_answer_lifeswitch_binding_v1;
DROP TABLE IF EXISTS lifeswitch_chat.account_timezone_v1;
DROP SCHEMA IF EXISTS lifeswitch_chat;

REVOKE lifeswitch_chat_reader_v1,lifeswitch_chat_binding_writer_v1 FROM brains_app;
DROP OWNED BY lifeswitch_chat_binding_writer_v1;
DROP OWNED BY lifeswitch_chat_reader_v1;
DROP ROLE IF EXISTS lifeswitch_chat_binding_writer_v1;
DROP ROLE IF EXISTS lifeswitch_chat_reader_v1;

COMMIT;
