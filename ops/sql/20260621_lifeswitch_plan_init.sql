-- LifeSwitch integrated plan schema.
-- Purpose:
--   One durable, current intervention plan per user.
--   This is intentionally cross-domain: body state, nutrition, training,
--   conditioning/activity, recovery, monitoring rules, and coach notes.

create extension if not exists pgcrypto;

create schema if not exists lifeswitch_plan;

create table if not exists lifeswitch_plan.plan_profile (
  plan_profile_id uuid primary key default gen_random_uuid(),

  owner_user_id uuid not null unique,

  -- High-level coaching frame.
  phase text not null default 'maintenance'
    check (phase in ('cut', 'maintenance', 'lean_gain', 'recomp', 'other')),
  phase_label text not null default '',
  primary_goal text not null default '',
  start_date date null,
  review_date date null,
  review_cadence text not null default 'weekly',

  -- Integrated plan sections.
  -- These stay JSONB for now because each section will evolve.
  body_state jsonb not null default '{}'::jsonb,
  nutrition_targets jsonb not null default '{}'::jsonb,
  training_targets jsonb not null default '{}'::jsonb,
  conditioning_targets jsonb not null default '{}'::jsonb,
  activity_targets jsonb not null default '{}'::jsonb,
  recovery_targets jsonb not null default '{}'::jsonb,
  monitoring_rules jsonb not null default '{}'::jsonb,

  coach_notes text not null default '',

  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists plan_profile_owner_active_idx
  on lifeswitch_plan.plan_profile (owner_user_id, is_active);

create index if not exists plan_profile_updated_idx
  on lifeswitch_plan.plan_profile (updated_at desc);

create table if not exists lifeswitch_plan.plan_profile_history (
  plan_profile_history_id uuid primary key default gen_random_uuid(),

  plan_profile_id uuid not null references lifeswitch_plan.plan_profile(plan_profile_id) on delete cascade,
  owner_user_id uuid not null,

  snapshot_reason text not null default 'manual_update',

  -- Full snapshot of the plan at the time of change.
  snapshot jsonb not null,

  created_at timestamptz not null default now()
);

create index if not exists plan_profile_history_owner_created_idx
  on lifeswitch_plan.plan_profile_history (owner_user_id, created_at desc);

create index if not exists plan_profile_history_profile_created_idx
  on lifeswitch_plan.plan_profile_history (plan_profile_id, created_at desc);

create or replace function lifeswitch_plan.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists plan_profile_set_updated_at on lifeswitch_plan.plan_profile;

create trigger plan_profile_set_updated_at
before update on lifeswitch_plan.plan_profile
for each row
execute function lifeswitch_plan.set_updated_at();
