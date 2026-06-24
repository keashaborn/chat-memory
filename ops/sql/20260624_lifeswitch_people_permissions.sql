-- LifeSwitch People relationship permission foundation.
-- This stores explicit permission scopes between known/private LifeSwitch relationships.
-- Enforcement will be wired into LifeSwitch plan/training/nutrition/measurement APIs later.

create table if not exists lifeswitch_people.relationship_permission (
  relationship_permission_id uuid primary key default gen_random_uuid(),

  relationship_id uuid not null
    references lifeswitch_people.relationship(relationship_id)
    on delete cascade,

  permission_scope text not null
    check (permission_scope in (
      'messages:send',
      'training:view',
      'nutrition:view',
      'measurements:view',
      'plan:view',
      'plan:comment',
      'plan:edit'
    )),

  permission_level text not null default 'none'
    check (permission_level in ('none','view','comment','edit','admin')),

  is_enabled boolean not null default false,

  notes text not null default '',

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  unique (relationship_id, permission_scope)
);

create index if not exists ix_lifeswitch_relationship_permission_relationship
  on lifeswitch_people.relationship_permission(relationship_id, is_enabled, permission_scope);

drop trigger if exists relationship_permission_set_updated_at on lifeswitch_people.relationship_permission;
create trigger relationship_permission_set_updated_at
before update on lifeswitch_people.relationship_permission
for each row execute function lifeswitch_people.set_updated_at();
