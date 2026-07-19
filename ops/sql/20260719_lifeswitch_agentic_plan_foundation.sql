\set ON_ERROR_STOP on

-- LifeSwitch agentic plan foundation.
-- Isolated from Memory V1: this migration creates only lifeswitch_agentic.*.
-- UUID primary keys are application-generated UUIDv7 values.

begin;

create schema if not exists lifeswitch_agentic;

create table lifeswitch_agentic.plan_versions (
  id uuid primary key,
  owner_user_id uuid not null,
  version_number bigint not null check (version_number > 0),
  status text not null check (status in ('active', 'superseded')),
  phase_code text,
  goal_type text,
  goal_summary text,
  starts_on date,
  target_date date,
  owner_timezone text not null,
  review_cadence_days integer
    check (review_cadence_days is null or review_cadence_days > 0),
  document jsonb not null,
  document_sha256 text not null
    check (document_sha256 ~ '^[0-9a-f]{64}$'),
  schema_version integer not null check (schema_version > 0),
  validation_version integer not null check (validation_version > 0),
  source_revision_id uuid,
  activation_type text not null
    check (activation_type in ('owner_approval', 'legacy_migration')),
  activated_by_actor_user_id uuid,
  migration_batch_id uuid,
  activated_at timestamptz not null,
  superseded_at timestamptz,
  created_at timestamptz not null default now(),
  unique (owner_user_id, version_number),
  check (
    (
      activation_type = 'owner_approval'
      and activated_by_actor_user_id is not null
      and migration_batch_id is null
      and source_revision_id is not null
    )
    or
    (
      activation_type = 'legacy_migration'
      and activated_by_actor_user_id is null
      and migration_batch_id is not null
      and source_revision_id is null
    )
  ),
  check (
    (status = 'active' and superseded_at is null)
    or (status = 'superseded' and superseded_at is not null)
  )
);

create unique index plan_versions_one_active_owner_idx
  on lifeswitch_agentic.plan_versions (owner_user_id)
  where status = 'active';

create index plan_versions_owner_history_idx
  on lifeswitch_agentic.plan_versions (owner_user_id, version_number desc);

create table lifeswitch_agentic.plan_owner_state (
  owner_user_id uuid primary key,
  active_plan_version_id uuid
    references lifeswitch_agentic.plan_versions (id) on delete restrict,
  next_version_number bigint not null default 1
    check (next_version_number > 0),
  updated_at timestamptz not null default now()
);

create unique index plan_owner_state_active_version_idx
  on lifeswitch_agentic.plan_owner_state (active_plan_version_id)
  where active_plan_version_id is not null;

create table lifeswitch_agentic.plan_revisions (
  id uuid primary key,
  owner_user_id uuid not null,
  base_plan_version_id uuid
    references lifeswitch_agentic.plan_versions (id) on delete restrict,
  supersedes_revision_id uuid
    references lifeswitch_agentic.plan_revisions (id) on delete restrict,
  state text not null check (
    state in (
      'draft', 'proposed', 'needs_changes', 'rejected',
      'withdrawn', 'conflicted', 'activated'
    )
  ),
  author_type text not null
    check (author_type in ('owner', 'coach', 'agent')),
  author_actor_user_id uuid,
  author_agent_task_id uuid,
  trigger_type text not null check (
    trigger_type in (
      'owner_request', 'coach_request',
      'analysis_recommendation', 'initial_plan'
    )
  ),
  recommendation_id uuid,
  proposed_document jsonb not null,
  proposed_document_sha256 text not null
    check (proposed_document_sha256 ~ '^[0-9a-f]{64}$'),
  schema_version integer not null check (schema_version > 0),
  validation_version integer,
  validation_result jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  proposed_at timestamptz,
  resolved_at timestamptz,
  activated_plan_version_id uuid
    references lifeswitch_agentic.plan_versions (id) on delete restrict,
  check (num_nonnulls(author_actor_user_id, author_agent_task_id) = 1),
  check (
    (
      author_type in ('owner', 'coach')
      and author_actor_user_id is not null
    )
    or
    (
      author_type = 'agent'
      and author_agent_task_id is not null
    )
  ),
  check (
    (trigger_type = 'initial_plan' and base_plan_version_id is null)
    or
    (trigger_type <> 'initial_plan' and base_plan_version_id is not null)
  ),
  check (
    (state = 'proposed')
    = (proposed_at is not null and resolved_at is null)
  ),
  check (
    (state = 'activated')
    = (activated_plan_version_id is not null)
  )
);

create index plan_revisions_base_plan_idx
  on lifeswitch_agentic.plan_revisions (base_plan_version_id)
  where base_plan_version_id is not null;

create index plan_revisions_supersedes_idx
  on lifeswitch_agentic.plan_revisions (supersedes_revision_id)
  where supersedes_revision_id is not null;

create index plan_revisions_owner_history_idx
  on lifeswitch_agentic.plan_revisions (owner_user_id, created_at desc);

create index plan_revisions_owner_open_idx
  on lifeswitch_agentic.plan_revisions (owner_user_id, state, created_at desc)
  where state in ('draft', 'proposed', 'needs_changes');

create index plan_revisions_activated_plan_idx
  on lifeswitch_agentic.plan_revisions (activated_plan_version_id)
  where activated_plan_version_id is not null;

create table lifeswitch_agentic.plan_revision_changes (
  id uuid primary key,
  plan_revision_id uuid not null
    references lifeswitch_agentic.plan_revisions (id) on delete restrict,
  field_path text not null,
  old_present boolean not null,
  old_value jsonb,
  new_present boolean not null,
  new_value jsonb,
  rationale text,
  created_at timestamptz not null default now(),
  unique (plan_revision_id, field_path),
  check (old_present or old_value is null),
  check (new_present or new_value is null)
);

create index plan_revision_changes_revision_idx
  on lifeswitch_agentic.plan_revision_changes (plan_revision_id);

create table lifeswitch_agentic.plan_revision_events (
  id bigint generated always as identity primary key,
  plan_revision_id uuid not null
    references lifeswitch_agentic.plan_revisions (id) on delete restrict,
  owner_user_id uuid not null,
  event_type text not null,
  actor_user_id uuid,
  agent_task_id uuid,
  prior_state text,
  new_state text,
  reason_code text,
  detail jsonb not null default '{}'::jsonb,
  request_id text,
  created_at timestamptz not null default now(),
  check (num_nonnulls(actor_user_id, agent_task_id) <= 1)
);

create index plan_revision_events_revision_idx
  on lifeswitch_agentic.plan_revision_events (plan_revision_id, id);

alter table lifeswitch_agentic.plan_versions
  add constraint plan_versions_source_revision_fkey
  foreign key (source_revision_id)
  references lifeswitch_agentic.plan_revisions (id)
  on delete restrict;

create index plan_versions_source_revision_idx
  on lifeswitch_agentic.plan_versions (source_revision_id)
  where source_revision_id is not null;

create function lifeswitch_agentic.reject_immutable_mutation()
returns trigger
language plpgsql
as $$
begin
  raise exception '% is append-only',
    tg_table_schema || '.' || tg_table_name
    using errcode = '55000';
end;
$$;

create function lifeswitch_agentic.protect_plan_version()
returns trigger
language plpgsql
as $$
begin
  if tg_op = 'DELETE' then
    raise exception '% is history-preserving',
      tg_table_schema || '.' || tg_table_name
      using errcode = '55000';
  end if;

  if old.status = 'active'
     and new.status = 'superseded'
     and old.superseded_at is null
     and new.superseded_at is not null
     and (to_jsonb(new) - array['status', 'superseded_at'])
         = (to_jsonb(old) - array['status', 'superseded_at']) then
    return new;
  end if;

  raise exception '% payload is immutable after activation',
    tg_table_schema || '.' || tg_table_name
    using errcode = '55000';
end;
$$;

create function lifeswitch_agentic.protect_plan_revision()
returns trigger
language plpgsql
as $$
begin
  if tg_op = 'DELETE' then
    raise exception '% is history-preserving',
      tg_table_schema || '.' || tg_table_name
      using errcode = '55000';
  end if;

  if new.owner_user_id <> old.owner_user_id
     or new.base_plan_version_id is distinct from old.base_plan_version_id
     or new.author_type <> old.author_type
     or new.author_actor_user_id is distinct from old.author_actor_user_id
     or new.author_agent_task_id is distinct from old.author_agent_task_id
     or new.trigger_type <> old.trigger_type
     or new.recommendation_id is distinct from old.recommendation_id then
    raise exception 'plan revision identity/provenance is immutable'
      using errcode = '55000';
  end if;

  if old.state = 'draft'
     and new.state in ('draft', 'proposed', 'withdrawn') then
    return new;
  end if;

  if old.state = 'proposed'
     and new.state in (
       'needs_changes', 'rejected', 'withdrawn',
       'conflicted', 'activated'
     )
     and new.proposed_document = old.proposed_document
     and new.proposed_document_sha256 = old.proposed_document_sha256
     and new.schema_version = old.schema_version
     and new.validation_version is not distinct from old.validation_version
     and new.validation_result is not distinct from old.validation_result then
    return new;
  end if;

  raise exception
    'invalid plan revision transition or proposed payload mutation'
    using errcode = '55000';
end;
$$;

create trigger plan_versions_protect_history
before update or delete on lifeswitch_agentic.plan_versions
for each row
execute function lifeswitch_agentic.protect_plan_version();

create trigger plan_revisions_protect_lifecycle
before update or delete on lifeswitch_agentic.plan_revisions
for each row
execute function lifeswitch_agentic.protect_plan_revision();

create trigger plan_revision_events_append_only
before update or delete on lifeswitch_agentic.plan_revision_events
for each row
execute function lifeswitch_agentic.reject_immutable_mutation();

commit;
