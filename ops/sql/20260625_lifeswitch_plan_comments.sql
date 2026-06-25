create table if not exists lifeswitch_plan.plan_comment (
  plan_comment_id uuid primary key default gen_random_uuid(),
  plan_profile_id uuid not null references lifeswitch_plan.plan_profile(plan_profile_id) on delete cascade,
  target_user_id uuid not null,
  author_user_id uuid not null,
  comment_text text not null,
  comment_kind text not null default 'comment',
  is_active boolean not null default true,
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists plan_comment_target_created_idx
  on lifeswitch_plan.plan_comment(target_user_id, created_at desc);

create index if not exists plan_comment_profile_created_idx
  on lifeswitch_plan.plan_comment(plan_profile_id, created_at desc);

drop trigger if exists plan_comment_set_updated_at on lifeswitch_plan.plan_comment;
create trigger plan_comment_set_updated_at
before update on lifeswitch_plan.plan_comment
for each row execute function lifeswitch_plan.set_updated_at();
