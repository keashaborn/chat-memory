BEGIN;

REVOKE ALL ON FUNCTION lifeswitch_chat.read_lifting_progression_summary_v3(uuid,date,date)
  FROM PUBLIC,brains_app,lifeswitch_chat_reader_v1;
DROP FUNCTION lifeswitch_chat.read_lifting_progression_summary_v3(uuid,date,date);

COMMIT;
