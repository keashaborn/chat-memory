\set ON_ERROR_STOP on

begin;

create schema if not exists lifeswitch_usage;

create table lifeswitch_usage.ai_usage_event_v1 (
  ai_usage_event_id uuid primary key default gen_random_uuid(),
  owner_user_id uuid not null,
  answer_id uuid not null,
  provider text not null check (provider = 'openai'),
  operation text not null check (
    operation in ('chat_response')
  ),
  source_channel text not null check (
    source_channel in ('chat', 'voice')
  ),
  provider_response_id text not null check (
    char_length(provider_response_id) between 1 and 240
  ),
  requested_model text not null check (
    char_length(requested_model) between 1 and 160
  ),
  returned_model text not null check (
    char_length(returned_model) between 1 and 160
  ),
  input_tokens bigint not null check (input_tokens >= 0),
  cached_input_tokens bigint not null default 0 check (
    cached_input_tokens >= 0 and cached_input_tokens <= input_tokens
  ),
  output_tokens bigint not null check (output_tokens >= 0),
  reasoning_output_tokens bigint not null default 0 check (
    reasoning_output_tokens >= 0
    and reasoning_output_tokens <= output_tokens
  ),
  total_tokens bigint not null check (
    total_tokens = input_tokens + output_tokens
  ),
  recorded_at timestamptz not null default now(),
  unique (provider, provider_response_id),
  unique (owner_user_id, answer_id)
);

create index ai_usage_event_v1_owner_recorded_idx
  on lifeswitch_usage.ai_usage_event_v1 (
    owner_user_id, recorded_at desc
  );

create index ai_usage_event_v1_owner_model_recorded_idx
  on lifeswitch_usage.ai_usage_event_v1 (
    owner_user_id, returned_model, recorded_at desc
  );

create function lifeswitch_usage.current_actor_user_id()
returns uuid
language sql
stable
set search_path = ''
as $$
  select nullif(current_setting('app.user_id', true), '')::uuid
$$;

alter table lifeswitch_usage.ai_usage_event_v1 enable row level security;
alter table lifeswitch_usage.ai_usage_event_v1 force row level security;

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

create function lifeswitch_usage.protect_ai_usage_event_v1()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'AI usage events are append-only'
    using errcode = '55000';
end;
$$;

create trigger ai_usage_event_v1_protect_history
before update or delete on lifeswitch_usage.ai_usage_event_v1
for each row
execute function lifeswitch_usage.protect_ai_usage_event_v1();

revoke all on schema lifeswitch_usage from public;
revoke all on all tables in schema lifeswitch_usage from public;
revoke all on all functions in schema lifeswitch_usage from public;

commit;
