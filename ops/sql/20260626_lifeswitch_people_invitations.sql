create table if not exists lifeswitch_people.invitation (
  invitation_id uuid primary key default gen_random_uuid(),
  token_hash text not null unique,

  created_by_user_id uuid not null,
  accepted_by_user_id uuid,

  relationship_kind text not null default 'friend',
  label text not null default '',
  notes text not null default '',

  status text not null default 'pending',
  expires_at timestamptz not null default (now() + interval '30 days'),
  accepted_at timestamptz,
  revoked_at timestamptz,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint invitation_status_check
    check (status in ('pending', 'accepted', 'revoked', 'expired')),

  constraint invitation_relationship_kind_check
    check (relationship_kind in ('friend', 'training_partner', 'plan_helper', 'coach')),

  constraint invitation_not_self_accept
    check (accepted_by_user_id is null or accepted_by_user_id <> created_by_user_id)
);

create index if not exists ix_invitation_created_by_status
  on lifeswitch_people.invitation(created_by_user_id, status, created_at desc);

create index if not exists ix_invitation_accepted_by
  on lifeswitch_people.invitation(accepted_by_user_id, accepted_at desc);

drop trigger if exists invitation_set_updated_at on lifeswitch_people.invitation;
create trigger invitation_set_updated_at
before update on lifeswitch_people.invitation
for each row execute function lifeswitch_people.set_updated_at();
