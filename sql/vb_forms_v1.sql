-- vb_forms_v1.sql
-- Minimal schema to support: chat -> form template -> publish -> fill -> DB

create table if not exists vb_form_templates (
  id            uuid primary key,
  owner_user_id text not null,
  name          text not null,
  status        text not null default 'draft', -- draft|published|archived
  created_at    timestamptz not null default now()
);

create table if not exists vb_form_versions (
  id           uuid primary key,
  template_id  uuid not null references vb_form_templates(id) on delete cascade,
  version      int  not null,
  json_schema  jsonb not null,
  ui_schema    jsonb not null default '{}'::jsonb,
  metadata     jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now(),
  unique(template_id, version)
);

create index if not exists vb_form_versions_template_version_idx
  on vb_form_versions(template_id, version desc);

create table if not exists vb_form_entries (
  id                 uuid primary key,
  owner_user_id       text not null,
  subject_id          text not null,
  template_version_id uuid not null references vb_form_versions(id),
  occurred_at         timestamptz not null default now(),
  data               jsonb not null,
  created_at         timestamptz not null default now()
);

create index if not exists vb_form_entries_subject_time_idx
  on vb_form_entries(subject_id, occurred_at desc);

create index if not exists vb_form_entries_owner_time_idx
  on vb_form_entries(owner_user_id, occurred_at desc);
