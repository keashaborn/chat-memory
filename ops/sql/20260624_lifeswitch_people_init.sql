-- LifeSwitch People / private messaging foundation.
-- Scope: friend/training-partner relationships + direct/group conversations.
-- No public community surface.

create schema if not exists lifeswitch_people;

create or replace function lifeswitch_people.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end $$;

create table if not exists lifeswitch_people.relationship (
  relationship_id uuid primary key default gen_random_uuid(),

  requester_user_id uuid not null,
  addressee_user_id uuid not null,

  status text not null default 'accepted'
    check (status in ('pending','accepted','blocked','revoked')),

  relationship_kind text not null default 'friend'
    check (relationship_kind in ('friend','training_partner','plan_helper','coach')),

  label text not null default '',
  notes text not null default '',

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint ck_relationship_not_self check (requester_user_id <> addressee_user_id)
);

create unique index if not exists ux_lifeswitch_relationship_pair
  on lifeswitch_people.relationship (
    least(requester_user_id, addressee_user_id),
    greatest(requester_user_id, addressee_user_id)
  );

create index if not exists ix_lifeswitch_relationship_requester
  on lifeswitch_people.relationship(requester_user_id, status, updated_at desc);

create index if not exists ix_lifeswitch_relationship_addressee
  on lifeswitch_people.relationship(addressee_user_id, status, updated_at desc);

drop trigger if exists relationship_set_updated_at on lifeswitch_people.relationship;
create trigger relationship_set_updated_at
before update on lifeswitch_people.relationship
for each row execute function lifeswitch_people.set_updated_at();

create table if not exists lifeswitch_people.conversation (
  conversation_id uuid primary key default gen_random_uuid(),

  conversation_kind text not null default 'direct'
    check (conversation_kind in ('direct','group')),

  created_by_user_id uuid not null,

  -- For direct conversations, these enforce one thread per pair.
  direct_user_low_id uuid,
  direct_user_high_id uuid,

  title text not null default '',
  is_active boolean not null default true,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint ck_direct_pair_not_self check (
    direct_user_low_id is null
    or direct_user_high_id is null
    or direct_user_low_id <> direct_user_high_id
  )
);

create unique index if not exists ux_lifeswitch_direct_conversation_pair
  on lifeswitch_people.conversation(direct_user_low_id, direct_user_high_id)
  where conversation_kind='direct'
    and direct_user_low_id is not null
    and direct_user_high_id is not null
    and is_active=true;

create index if not exists ix_lifeswitch_conversation_updated
  on lifeswitch_people.conversation(updated_at desc);

drop trigger if exists conversation_set_updated_at on lifeswitch_people.conversation;
create trigger conversation_set_updated_at
before update on lifeswitch_people.conversation
for each row execute function lifeswitch_people.set_updated_at();

create table if not exists lifeswitch_people.conversation_member (
  conversation_member_id uuid primary key default gen_random_uuid(),

  conversation_id uuid not null references lifeswitch_people.conversation(conversation_id) on delete cascade,
  user_id uuid not null,

  member_role text not null default 'member'
    check (member_role in ('owner','member','helper','coach')),

  last_read_at timestamptz,
  is_active boolean not null default true,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  unique(conversation_id, user_id)
);

create index if not exists ix_lifeswitch_conversation_member_user
  on lifeswitch_people.conversation_member(user_id, is_active, updated_at desc);

create index if not exists ix_lifeswitch_conversation_member_conversation
  on lifeswitch_people.conversation_member(conversation_id, is_active);

drop trigger if exists conversation_member_set_updated_at on lifeswitch_people.conversation_member;
create trigger conversation_member_set_updated_at
before update on lifeswitch_people.conversation_member
for each row execute function lifeswitch_people.set_updated_at();

create table if not exists lifeswitch_people.message (
  message_id uuid primary key default gen_random_uuid(),

  conversation_id uuid not null references lifeswitch_people.conversation(conversation_id) on delete cascade,
  author_user_id uuid not null,

  body text not null,
  body_format text not null default 'plain'
    check (body_format in ('plain','markdown')),

  metadata jsonb not null default '{}'::jsonb,

  is_deleted boolean not null default false,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists ix_lifeswitch_message_conversation_created
  on lifeswitch_people.message(conversation_id, created_at desc);

create index if not exists ix_lifeswitch_message_author
  on lifeswitch_people.message(author_user_id, created_at desc);

drop trigger if exists message_set_updated_at on lifeswitch_people.message;
create trigger message_set_updated_at
before update on lifeswitch_people.message
for each row execute function lifeswitch_people.set_updated_at();
