\set ON_ERROR_STOP on

begin;

do $$
begin
  if not exists (
    select 1 from pg_roles where rolname='lifeswitch_usage_writer_v1'
  ) then
    create role lifeswitch_usage_writer_v1
      nologin
      inherit
      nosuperuser
      nocreatedb
      nocreaterole
      noreplication
      nobypassrls;
  end if;
  if not exists (
    select 1 from pg_roles where rolname='lifeswitch_usage_admin_v1'
  ) then
    create role lifeswitch_usage_admin_v1
      nologin
      inherit
      nosuperuser
      nocreatedb
      nocreaterole
      noreplication
      nobypassrls;
  end if;
  execute format(
    'grant lifeswitch_usage_writer_v1 to %I',
    current_user
  );
  execute format(
    'grant lifeswitch_usage_admin_v1 to %I',
    current_user
  );
end
$$;

alter table lifeswitch_usage.ai_usage_event_v1
  disable trigger ai_usage_event_v1_protect_history;

alter table lifeswitch_usage.ai_usage_event_v1
  add column helper text,
  add column idempotency_key text,
  add column event_schema_version smallint;

update lifeswitch_usage.ai_usage_event_v1
set
  helper='openai_chat_completions_v1',
  idempotency_key=answer_id::text,
  event_schema_version=1;

alter table lifeswitch_usage.ai_usage_event_v1
  alter column helper set not null,
  alter column idempotency_key set not null,
  alter column event_schema_version set not null,
  alter column event_schema_version set default 1;

alter table lifeswitch_usage.ai_usage_event_v1
  add constraint ai_usage_event_v1_helper_check check (
    char_length(helper) between 1 and 160
  ),
  add constraint ai_usage_event_v1_idempotency_key_check check (
    char_length(idempotency_key) between 1 and 240
  ),
  add constraint ai_usage_event_v1_event_schema_version_check check (
    event_schema_version = 1
  ),
  add constraint ai_usage_event_v1_owner_operation_idempotency_key_key
    unique (owner_user_id,operation,idempotency_key);

alter table lifeswitch_usage.ai_usage_event_v1
  enable trigger ai_usage_event_v1_protect_history;

drop policy ai_usage_event_v1_owner_select
  on lifeswitch_usage.ai_usage_event_v1;
drop policy ai_usage_event_v1_owner_insert
  on lifeswitch_usage.ai_usage_event_v1;

create policy ai_usage_event_v1_owner_select
on lifeswitch_usage.ai_usage_event_v1
for select
to lifeswitch_usage_writer_v1
using (
  owner_user_id = lifeswitch_usage.current_actor_user_id()
);

create policy ai_usage_event_v1_owner_insert
on lifeswitch_usage.ai_usage_event_v1
for insert
to lifeswitch_usage_writer_v1
with check (
  owner_user_id = lifeswitch_usage.current_actor_user_id()
);

create policy ai_usage_event_v1_admin_select
on lifeswitch_usage.ai_usage_event_v1
for select
to lifeswitch_usage_admin_v1
using (true);

alter table lifeswitch_usage.ai_usage_event_v1
  enable row level security;
alter table lifeswitch_usage.ai_usage_event_v1
  force row level security;

grant usage on schema lifeswitch_usage
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant execute on function lifeswitch_usage.current_actor_user_id()
  to lifeswitch_usage_writer_v1;
grant select,insert on lifeswitch_usage.ai_usage_event_v1
  to lifeswitch_usage_writer_v1;
grant select on lifeswitch_usage.ai_usage_event_v1
  to lifeswitch_usage_admin_v1;

grant usage on schema lifeswitch_nutrition
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant select (
  nutrition_day_id,
  owner_user_id,
  day,
  completed_at
) on lifeswitch_nutrition.nutrition_day
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant select (
  nutrition_entry_id,
  nutrition_day_id
) on lifeswitch_nutrition.nutrition_entry
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;

grant usage on schema lifeswitch_training
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant select (
  training_session_id,
  owner_user_id,
  day,
  finished_at,
  is_active
) on lifeswitch_training.training_session_current_v
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant select (
  training_session_id,
  owner_user_id,
  is_active
) on lifeswitch_training.training_set_log
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant select (
  owner_user_id,
  day,
  is_active
) on lifeswitch_training.conditioning_session_current_v
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;

revoke all on schema lifeswitch_usage from public;
revoke all on lifeswitch_usage.ai_usage_event_v1 from public;
revoke all on all functions in schema lifeswitch_usage from public;

commit;
