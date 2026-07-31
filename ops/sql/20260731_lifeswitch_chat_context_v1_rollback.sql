BEGIN;

DROP TRIGGER IF EXISTS final_answer_lifeswitch_binding_immutable_v1
  ON lifeswitch_chat.final_answer_lifeswitch_binding_v1;
DROP FUNCTION IF EXISTS lifeswitch_chat.reject_binding_mutation_v1();
DROP TABLE IF EXISTS lifeswitch_chat.final_answer_lifeswitch_binding_v1;
DROP FUNCTION IF EXISTS lifeswitch_chat.read_exercise_progression_v1(uuid,date,date,text);
DROP FUNCTION IF EXISTS lifeswitch_chat.read_training_day_v1(uuid,date);
DROP FUNCTION IF EXISTS lifeswitch_chat.read_measurement_observations_v1(uuid,date,date);
DROP FUNCTION IF EXISTS lifeswitch_chat.read_conditioning_sessions_v1(uuid,date,date);
DROP FUNCTION IF EXISTS lifeswitch_chat.read_resistance_sessions_v1(uuid,date,date);
DROP FUNCTION IF EXISTS lifeswitch_chat.read_nutrition_daily_v1(uuid,date,date);
DROP FUNCTION IF EXISTS lifeswitch_chat.read_plan_v1(uuid);
DROP FUNCTION IF EXISTS lifeswitch_chat.read_owner_timezone_v1(uuid);
DROP FUNCTION IF EXISTS lifeswitch_chat.whitelist_plan_document_v1(jsonb);
DROP FUNCTION IF EXISTS lifeswitch_chat.whitelist_target_section_v1(jsonb,text[]);
DROP FUNCTION IF EXISTS lifeswitch_chat.whitelist_target_value_v1(jsonb);
DROP FUNCTION IF EXISTS lifeswitch_chat.resolve_owner_read_context_v1(uuid);
DROP FUNCTION IF EXISTS lifeswitch_chat.end_owner_read_context_v1(uuid);
DROP FUNCTION IF EXISTS lifeswitch_chat.begin_owner_read_context_v1(uuid,uuid,text,text);
DROP TABLE IF EXISTS lifeswitch_chat.account_timezone_v1;
DROP TABLE IF EXISTS lifeswitch_chat.owner_read_context_v1;
DROP SCHEMA IF EXISTS lifeswitch_chat;

REVOKE lifeswitch_chat_reader_v1,lifeswitch_chat_binding_writer_v1 FROM brains_app;
DROP OWNED BY lifeswitch_chat_binding_writer_v1;
DROP OWNED BY lifeswitch_chat_reader_v1;
DROP ROLE IF EXISTS lifeswitch_chat_binding_writer_v1;
DROP ROLE IF EXISTS lifeswitch_chat_reader_v1;

COMMIT;
