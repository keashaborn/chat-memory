BEGIN;

REVOKE ALL ON lifeswitch_training.training_set_effective_role_v1
  FROM PUBLIC,brains_app,lifeswitch_chat_reader_v1,lifeswitch_chat_binding_writer_v1;
DROP VIEW lifeswitch_training.training_set_effective_role_v1;

COMMIT;
