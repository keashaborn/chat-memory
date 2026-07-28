\set ON_ERROR_STOP on

begin;

revoke select on lifeswitch_usage.ai_actor_registry_v1
  from lifeswitch_usage_admin_v1;

drop policy if exists ai_actor_registry_v1_admin_select
  on lifeswitch_usage.ai_actor_registry_v1;
drop trigger if exists ai_actor_registry_v1_protect_history
  on lifeswitch_usage.ai_actor_registry_v1;
drop function if exists
  lifeswitch_usage.protect_ai_actor_registry_v1();
drop table if exists lifeswitch_usage.ai_actor_registry_v1;

commit;
