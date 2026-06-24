-- LifeSwitch People display-name mirror.
-- This is a local app-facing mirror of known Supabase users for LifeSwitch People UI.
-- Supabase Auth remains the identity source; this table is for display/search only.

create table if not exists lifeswitch_people.user_profile (
  user_id uuid primary key,
  display_name text not null default '',
  email text not null default '',
  source text not null default 'manual_seed',
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

drop trigger if exists user_profile_set_updated_at on lifeswitch_people.user_profile;
create trigger user_profile_set_updated_at
before update on lifeswitch_people.user_profile
for each row execute function lifeswitch_people.set_updated_at();

create index if not exists ix_lifeswitch_people_user_profile_name
  on lifeswitch_people.user_profile(lower(display_name));

create index if not exists ix_lifeswitch_people_user_profile_email
  on lifeswitch_people.user_profile(lower(email));

insert into lifeswitch_people.user_profile (user_id, display_name, email, source, is_active)
values
  ('1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid, 'Eric', 'elund@seebx.com', 'manual_seed', true),
  ('e049fcde-655a-4377-af91-e85fd98b4d8c'::uuid, 'Lucifer', 'elund@lifeswitch.com', 'manual_seed', true),
  ('818b60b9-89bd-442a-998c-fc1924184dfc'::uuid, 'Heidi Lynn Lund', 'heidilynnlund@gmail.com', 'manual_seed', true),
  ('8bda2183-b595-4a91-af81-18cafc90deda'::uuid, 'Karim', 'karim.chalhoub@gmail.com', 'manual_seed', true),
  ('673d64a3-c4ba-4d1c-89e3-e0c579022fad'::uuid, 'Kelly Klaus', 'kelly@musicugreenbay.com', 'manual_seed', true)
on conflict (user_id) do update set
  display_name=excluded.display_name,
  email=excluded.email,
  source=excluded.source,
  is_active=excluded.is_active,
  updated_at=now();
