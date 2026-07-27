\set ON_ERROR_STOP on

begin;

create table lifeswitch_agentic.recovery_adjustments (
  id uuid primary key,
  owner_user_id uuid not null,
  reason_code text not null check (
    reason_code in ('surgery_recovery', 'illness', 'injury', 'other')
  ),
  note text not null default '' check (char_length(note) <= 1000),
  nutrition_starts_on date,
  nutrition_ends_on date,
  strength_starts_on date,
  strength_ends_on date,
  created_by_actor_user_id uuid not null,
  created_at timestamptz not null default now(),
  stopped_on date,
  stopped_by_actor_user_id uuid,
  stopped_at timestamptz,
  unique (owner_user_id, id),
  check (
    (nutrition_starts_on is null and nutrition_ends_on is null)
    or (
      nutrition_starts_on is not null
      and nutrition_ends_on is not null
      and nutrition_ends_on >= nutrition_starts_on
      and nutrition_ends_on <= nutrition_starts_on + 365
    )
  ),
  check (
    (strength_starts_on is null and strength_ends_on is null)
    or (
      strength_starts_on is not null
      and strength_ends_on is not null
      and strength_ends_on >= strength_starts_on
      and strength_ends_on <= strength_starts_on + 365
    )
  ),
  check (nutrition_starts_on is not null or strength_starts_on is not null),
  check (
    num_nonnulls(stopped_on, stopped_by_actor_user_id, stopped_at) in (0, 3)
  )
);

create index recovery_adjustments_owner_history_idx
  on lifeswitch_agentic.recovery_adjustments (owner_user_id, created_at desc);

create index recovery_adjustments_owner_nutrition_period_idx
  on lifeswitch_agentic.recovery_adjustments (
    owner_user_id, nutrition_starts_on, nutrition_ends_on
  )
  where nutrition_starts_on is not null;

create index recovery_adjustments_owner_strength_period_idx
  on lifeswitch_agentic.recovery_adjustments (
    owner_user_id, strength_starts_on, strength_ends_on
  )
  where strength_starts_on is not null;

create function lifeswitch_agentic.protect_recovery_adjustment()
returns trigger
language plpgsql
as $$
begin
  if tg_op = 'DELETE' then
    raise exception '% is history-preserving',
      tg_table_schema || '.' || tg_table_name
      using errcode = '55000';
  end if;

  if old.stopped_at is null
     and new.stopped_at is not null
     and new.stopped_on is not null
     and new.stopped_by_actor_user_id is not null
     and (to_jsonb(new) - array[
       'stopped_on', 'stopped_by_actor_user_id', 'stopped_at'
     ]) = (to_jsonb(old) - array[
       'stopped_on', 'stopped_by_actor_user_id', 'stopped_at'
     ]) then
    return new;
  end if;

  raise exception 'recovery adjustment payload is immutable'
    using errcode = '55000';
end;
$$;

create trigger recovery_adjustments_protect_history
before update or delete on lifeswitch_agentic.recovery_adjustments
for each row
execute function lifeswitch_agentic.protect_recovery_adjustment();

commit;
