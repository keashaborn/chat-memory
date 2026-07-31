BEGIN;

REVOKE ALL ON FUNCTION lifeswitch_chat.read_lifting_progression_summary_v1(uuid,date,date)
  FROM PUBLIC,brains_app,lifeswitch_chat_reader_v1;
REVOKE ALL ON FUNCTION lifeswitch_chat.read_exercise_frequency_v1(uuid,date,date)
  FROM PUBLIC,brains_app,lifeswitch_chat_reader_v1;
DROP FUNCTION lifeswitch_chat.read_lifting_progression_summary_v1(uuid,date,date);
DROP FUNCTION lifeswitch_chat.read_exercise_frequency_v1(uuid,date,date);

COMMIT;
