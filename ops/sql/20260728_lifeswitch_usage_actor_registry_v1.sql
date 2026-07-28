\set ON_ERROR_STOP on

begin;

create table lifeswitch_usage.ai_actor_registry_v1 (
  actor_user_id uuid primary key,
  actor_kind text not null check (
    actor_kind in ('synthetic','system')
  ),
  workload_key text not null unique check (
    workload_key ~ '^[a-z][a-z0-9_]{2,63}$'
  ),
  display_label text not null check (
    char_length(display_label) between 1 and 120
  ),
  event_schema_version smallint not null default 1 check (
    event_schema_version = 1
  ),
  registered_at timestamptz not null default now()
);

alter table lifeswitch_usage.ai_actor_registry_v1
  enable row level security;
alter table lifeswitch_usage.ai_actor_registry_v1
  force row level security;

create policy ai_actor_registry_v1_admin_select
on lifeswitch_usage.ai_actor_registry_v1
for select
to lifeswitch_usage_admin_v1
using (true);

create function lifeswitch_usage.protect_ai_actor_registry_v1()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'AI actor classifications are append-only'
    using errcode = '55000';
end;
$$;

create trigger ai_actor_registry_v1_protect_history
before update or delete on lifeswitch_usage.ai_actor_registry_v1
for each row
execute function lifeswitch_usage.protect_ai_actor_registry_v1();

insert into lifeswitch_usage.ai_actor_registry_v1 (
  actor_user_id,
  actor_kind,
  workload_key,
  display_label
) values (
  '1b8daceb-e78d-47a6-bfc0-2d6b8e24b33f',
  'synthetic',
  'voice_synthetic_canary',
  'Voice synthetic canary'
);

grant select on lifeswitch_usage.ai_actor_registry_v1
  to lifeswitch_usage_admin_v1;

revoke all on lifeswitch_usage.ai_actor_registry_v1 from public;
revoke all on function
  lifeswitch_usage.protect_ai_actor_registry_v1()
  from public;

commit;
