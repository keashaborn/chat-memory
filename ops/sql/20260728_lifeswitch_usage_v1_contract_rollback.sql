\set ON_ERROR_STOP on

begin;

drop policy if exists ai_usage_event_v1_admin_select
  on lifeswitch_usage.ai_usage_event_v1;
drop policy if exists ai_usage_event_v1_owner_select
  on lifeswitch_usage.ai_usage_event_v1;
drop policy if exists ai_usage_event_v1_owner_insert
  on lifeswitch_usage.ai_usage_event_v1;

create policy ai_usage_event_v1_owner_select
on lifeswitch_usage.ai_usage_event_v1
for select
using (
  owner_user_id = lifeswitch_usage.current_actor_user_id()
);

create policy ai_usage_event_v1_owner_insert
on lifeswitch_usage.ai_usage_event_v1
for insert
with check (
  owner_user_id = lifeswitch_usage.current_actor_user_id()
);

revoke select,insert on lifeswitch_usage.ai_usage_event_v1
  from lifeswitch_usage_writer_v1;
revoke select on lifeswitch_usage.ai_usage_event_v1
  from lifeswitch_usage_admin_v1;
revoke execute on function lifeswitch_usage.current_actor_user_id()
  from lifeswitch_usage_writer_v1;
revoke usage on schema lifeswitch_usage
  from lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;

revoke select (
  nutrition_day_id,
  owner_user_id,
  day,
  completed_at
) on lifeswitch_nutrition.nutrition_day
  from lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
revoke select (
  nutrition_entry_id,
  nutrition_day_id
) on lifeswitch_nutrition.nutrition_entry
  from lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
revoke usage on schema lifeswitch_nutrition
  from lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;

revoke select (
  training_session_id,
  owner_user_id,
  day,
  finished_at,
  is_active
) on lifeswitch_training.training_session_current_v
  from lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
revoke select (
  training_session_id,
  owner_user_id,
  is_active
) on lifeswitch_training.training_set_log
  from lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
revoke select (
  owner_user_id,
  day,
  is_active
) on lifeswitch_training.conditioning_session_current_v
  from lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
revoke usage on schema lifeswitch_training
  from lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;

alter table lifeswitch_usage.ai_usage_event_v1
  drop constraint if exists
    ai_usage_event_v1_owner_operation_idempotency_key_key,
  drop constraint if exists ai_usage_event_v1_event_schema_version_check,
  drop constraint if exists ai_usage_event_v1_idempotency_key_check,
  drop constraint if exists ai_usage_event_v1_helper_check,
  drop column if exists event_schema_version,
  drop column if exists idempotency_key,
  drop column if exists helper;

do $$
begin
  execute format(
    'revoke lifeswitch_usage_writer_v1 from %I',
    current_user
  );
  execute format(
    'revoke lifeswitch_usage_admin_v1 from %I',
    current_user
  );
end
$$;

drop role lifeswitch_usage_writer_v1;
drop role lifeswitch_usage_admin_v1;

commit;
