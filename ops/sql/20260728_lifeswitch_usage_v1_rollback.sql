\set ON_ERROR_STOP on

begin;

drop trigger if exists ai_usage_event_v1_protect_history
  on lifeswitch_usage.ai_usage_event_v1;
drop function if exists lifeswitch_usage.protect_ai_usage_event_v1();
drop table if exists lifeswitch_usage.ai_usage_event_v1;
drop function if exists lifeswitch_usage.current_actor_user_id();
drop schema if exists lifeswitch_usage;

commit;
