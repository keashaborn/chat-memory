create table if not exists lifeswitch_training.workout_template_share (
  workout_template_share_id uuid primary key default gen_random_uuid(),
  token_hash text not null unique,

  created_by_user_id uuid not null,
  workout_template_id uuid not null,

  status text not null default 'active',
  label text not null default '',
  notes text not null default '',
  expires_at timestamptz not null default (now() + interval '90 days'),
  revoked_at timestamptz,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint workout_template_share_status_check
    check (status in ('active', 'revoked', 'expired'))
);

create index if not exists ix_workout_template_share_creator_status
  on lifeswitch_training.workout_template_share(created_by_user_id, status, created_at desc);

create index if not exists ix_workout_template_share_template
  on lifeswitch_training.workout_template_share(workout_template_id);

drop trigger if exists trg_workout_template_share_updated_at on lifeswitch_training.workout_template_share;
create trigger trg_workout_template_share_updated_at
before update on lifeswitch_training.workout_template_share
for each row execute function lifeswitch_training.tg_set_updated_at();
