#!/usr/bin/env bash
set -euo pipefail

live_container="brains-postgres-1"
candidate="/tmp/chat-memory-admin-usage-v1"
clone_name="lifeswitch-usage-v1-clone-${$}"
clone_password="clone-only-usage-v1"
clone_image="$(docker inspect -f '{{.Config.Image}}' "$live_container")"
clone_tmp="$(mktemp -d /tmp/lifeswitch-usage-v1-clone.XXXXXX)"
schema_dump="$clone_tmp/relevant-schema.sql"

cleanup() {
  docker rm -f "$clone_name" >/dev/null 2>&1 || true
  rm -rf "$clone_tmp"
}
trap cleanup EXIT

production_before="$(
  docker exec "$live_container" psql -X -U sage -d memory -At -F '|' -c \
    "select count(*),coalesce(sum(total_tokens),0),min(recorded_at),max(recorded_at) from lifeswitch_usage.ai_usage_event_v1"
)"

docker exec "$live_container" pg_dump \
  -U sage \
  -d memory \
  --schema-only \
  --no-owner \
  --no-privileges \
  --schema=catalog_dev \
  --schema=lifeswitch_usage \
  --schema=lifeswitch_nutrition \
  --schema=lifeswitch_training \
  > "$schema_dump"

docker run -d \
  --name "$clone_name" \
  -e POSTGRES_USER=sage \
  -e POSTGRES_PASSWORD="$clone_password" \
  -e POSTGRES_DB=memory \
  -p 127.0.0.1::5432 \
  "$clone_image" >/dev/null

for _attempt in $(seq 1 30); do
  if docker exec "$clone_name" pg_isready -U sage -d memory >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$clone_name" pg_isready -U sage -d memory >/dev/null

docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  -c "create extension if not exists citext with schema public;
      create extension if not exists pg_trgm with schema public;
      create extension if not exists pgcrypto with schema public;
      create extension if not exists unaccent with schema public"

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory < "$schema_dump"
docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260728_lifeswitch_usage_v1_contract.sql"

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory <<'SQL'
insert into lifeswitch_nutrition.meal (
  meal_id,owner_user_id,name
) values (
  '91000000-0000-4000-8000-000000000001',
  '90000000-0000-4000-8000-000000000001',
  'Synthetic fixture'
);
insert into lifeswitch_nutrition.nutrition_day (
  nutrition_day_id,owner_user_id,day,completed_at
) values (
  '92000000-0000-4000-8000-000000000001',
  '90000000-0000-4000-8000-000000000001',
  current_date,
  now()
);
insert into lifeswitch_nutrition.nutrition_entry (
  nutrition_entry_id,nutrition_day_id,meal_id
) values (
  '93000000-0000-4000-8000-000000000001',
  '92000000-0000-4000-8000-000000000001',
  '91000000-0000-4000-8000-000000000001'
);
update lifeswitch_nutrition.nutrition_day
set completed_at=now()
where nutrition_day_id='92000000-0000-4000-8000-000000000001';
insert into lifeswitch_training.training_session (
  training_session_id,owner_user_id,day,name,started_at,finished_at,
  recorded_by_user_id,source_snapshot,snapshot_schema_version,
  snapshot_quality,workout_role_snapshot
) values (
  '94000000-0000-4000-8000-000000000001',
  '90000000-0000-4000-8000-000000000001',
  current_date,
  'Synthetic fixture',
  now() - interval '30 minutes',
  now(),
  '90000000-0000-4000-8000-000000000001',
  '{"fixture":true}'::jsonb,
  1,
  'synthetic',
  'strength'
);
insert into lifeswitch_training.training_set_log (
  training_set_log_id,training_session_id,owner_user_id,exercise_id,
  exercise_name,capture_role,load_unit,source_snapshot,
  snapshot_schema_version,snapshot_quality
) values (
  '95000000-0000-4000-8000-000000000001',
  '94000000-0000-4000-8000-000000000001',
  '90000000-0000-4000-8000-000000000001',
  'synthetic',
  'Synthetic fixture',
  'strength',
  'lb',
  '{"fixture":true}'::jsonb,
  1,
  'synthetic'
);
insert into lifeswitch_training.conditioning_session_log (
  conditioning_session_log_id,owner_user_id,day,name,
  recorded_by_user_id,source_snapshot,snapshot_schema_version,
  snapshot_quality
) values (
  '96000000-0000-4000-8000-000000000001',
  '90000000-0000-4000-8000-000000000002',
  current_date,
  'Synthetic fixture',
  '90000000-0000-4000-8000-000000000002',
  '{"fixture":true}'::jsonb,
  1,
  'synthetic'
);

begin;
set local role lifeswitch_usage_writer_v1;
select set_config(
  'app.user_id',
  '90000000-0000-4000-8000-000000000001',
  true
);
insert into lifeswitch_usage.ai_usage_event_v1 (
  owner_user_id,answer_id,provider,operation,helper,source_channel,
  provider_response_id,requested_model,returned_model,input_tokens,
  cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,
  idempotency_key,event_schema_version
) values (
  '90000000-0000-4000-8000-000000000001',
  '97000000-0000-4000-8000-000000000001',
  'openai','chat_response','openai_chat_completions_v1','chat',
  'clone-response-1','gpt-test','gpt-test',10,2,5,1,15,
  '97000000-0000-4000-8000-000000000001',1
);
commit;

begin;
set local role lifeswitch_usage_writer_v1;
select set_config(
  'app.user_id',
  '90000000-0000-4000-8000-000000000002',
  true
);
insert into lifeswitch_usage.ai_usage_event_v1 (
  owner_user_id,answer_id,provider,operation,helper,source_channel,
  provider_response_id,requested_model,returned_model,input_tokens,
  cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,
  idempotency_key,event_schema_version
) values (
  '90000000-0000-4000-8000-000000000002',
  '97000000-0000-4000-8000-000000000002',
  'openai','chat_response','openai_chat_completions_v1','voice',
  'clone-response-2','gpt-test','gpt-test',20,0,10,4,30,
  '97000000-0000-4000-8000-000000000002',1
);
commit;

select 1 / case when (
  select count(*) from pg_attribute a
  join pg_class c on c.oid=a.attrelid
  join pg_namespace n on n.oid=c.relnamespace
  where n.nspname='lifeswitch_usage'
    and c.relname='ai_usage_event_v1'
    and a.attname in ('helper','idempotency_key','event_schema_version')
    and a.attnotnull
    and not a.attisdropped
)=3 then 1 else 0 end;
select 1 / case when (
  select count(*) from pg_roles
  where rolname in (
    'lifeswitch_usage_writer_v1',
    'lifeswitch_usage_admin_v1'
  ) and not rolbypassrls and not rolcanlogin
)=2 then 1 else 0 end;
select 1 / case when (
  select count(*) from pg_policies
  where schemaname='lifeswitch_usage'
    and tablename='ai_usage_event_v1'
    and policyname in (
      'ai_usage_event_v1_owner_select',
      'ai_usage_event_v1_owner_insert',
      'ai_usage_event_v1_admin_select'
    )
)=3 then 1 else 0 end;
select 1 / case when not exists (
  select 1
  from pg_class c
  join pg_namespace n on n.oid=c.relnamespace
  cross join lateral aclexplode(
    coalesce(c.relacl,acldefault('r',c.relowner))
  ) acl
  where n.nspname='lifeswitch_usage'
    and c.relname='ai_usage_event_v1'
    and acl.grantee=0
    and acl.privilege_type='SELECT'
) then 1 else 0 end;
SQL

if docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory >/dev/null 2>&1 <<'SQL'
begin;
set local role lifeswitch_usage_writer_v1;
select set_config(
  'app.user_id',
  '90000000-0000-4000-8000-000000000001',
  true
);
insert into lifeswitch_usage.ai_usage_event_v1 (
  owner_user_id,answer_id,provider,operation,helper,source_channel,
  provider_response_id,requested_model,returned_model,input_tokens,
  cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,
  idempotency_key,event_schema_version
) values (
  '90000000-0000-4000-8000-000000000002',
  '97000000-0000-4000-8000-000000000003',
  'openai','chat_response','openai_chat_completions_v1','chat',
  'clone-response-cross-owner','gpt-test','gpt-test',1,0,1,0,2,
  '97000000-0000-4000-8000-000000000003',1
);
commit;
SQL
then
  printf 'cross-owner insert unexpectedly succeeded\n' >&2
  exit 1
fi

if docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory >/dev/null 2>&1 <<'SQL'
begin;
set local role lifeswitch_usage_admin_v1;
select notes from lifeswitch_nutrition.nutrition_day limit 1;
rollback;
SQL
then
  printf 'admin aggregate role read a prohibited nutrition column\n' >&2
  exit 1
fi

if docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory >/dev/null 2>&1 <<'SQL'
update lifeswitch_usage.ai_usage_event_v1
set total_tokens=total_tokens
where provider_response_id='clone-response-1';
SQL
then
  printf 'append-only update unexpectedly succeeded\n' >&2
  exit 1
fi

clone_port="$(
  docker port "$clone_name" 5432/tcp | sed -n '1s/.*://p'
)"
clone_dsn="postgresql://sage:${clone_password}@127.0.0.1:${clone_port}/memory"

POSTGRES_DSN="$clone_dsn" \
PYTHONPATH="$candidate" \
/opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
from uuid import UUID

from rag_engine.usage_ledger_v1 import (
    AdminUsageUsersRequestV1,
    build_admin_usage_overview_v1,
    build_admin_usage_user_detail_v1,
    build_admin_usage_users_v1,
)
import os


async def main() -> None:
    dsn = os.environ["POSTGRES_DSN"]
    secret = "clone-cursor-secret-that-is-long-enough"
    overview = await build_admin_usage_overview_v1(
        dsn=dsn,
        window_days=30,
    )
    assert overview["active_users"] == 2, overview
    assert overview["ai"]["requests"] == 2, overview
    assert overview["ai"]["total_tokens"] == 45, overview
    assert overview["nutrition"]["days_logged"] == 1, overview
    assert overview["nutrition"]["days_completed"] == 1, overview
    assert overview["training"]["strength_sessions"] == 1, overview
    assert overview["training"]["conditioning_sessions"] == 1, overview

    first = await build_admin_usage_users_v1(
        dsn=dsn,
        request=AdminUsageUsersRequestV1(limit=1),
        cursor_secret=secret,
    )
    assert len(first["items"]) == 1, first
    assert first["has_more"] is True, first
    assert first["next_cursor"], first
    second = await build_admin_usage_users_v1(
        dsn=dsn,
        request=AdminUsageUsersRequestV1(
            limit=1,
            cursor=first["next_cursor"],
        ),
        cursor_secret=secret,
    )
    assert len(second["items"]) == 1, second
    assert second["items"][0]["user_id"] != first["items"][0]["user_id"]

    detail = await build_admin_usage_user_detail_v1(
        dsn=dsn,
        target_user_id=UUID("90000000-0000-4000-8000-000000000001"),
        window_days=30,
    )
    assert detail["summary"]["ai"]["total_tokens"] == 15, detail
    assert detail["summary"]["nutrition"]["days_logged"] == 1, detail
    assert detail["summary"]["training"]["strength_sessions"] == 1, detail
    assert len(detail["daily"]) == 1, detail

    print(
        "CLONE_QUERY_ASSERTIONS "
        f"active_users={overview['active_users']} "
        f"requests={overview['ai']['requests']} "
        f"tokens={overview['ai']['total_tokens']} "
        f"pages={len(first['items']) + len(second['items'])}"
    )


asyncio.run(main())
PY

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260728_lifeswitch_usage_v1_contract_rollback.sql"

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory <<'SQL'
select 1 / case when not exists (
  select 1 from pg_attribute a
  join pg_class c on c.oid=a.attrelid
  join pg_namespace n on n.oid=c.relnamespace
  where n.nspname='lifeswitch_usage'
    and c.relname='ai_usage_event_v1'
    and a.attname in ('helper','idempotency_key','event_schema_version')
    and not a.attisdropped
) then 1 else 0 end;
select 1 / case when not exists (
  select 1 from pg_roles
  where rolname in (
    'lifeswitch_usage_writer_v1',
    'lifeswitch_usage_admin_v1'
  )
) then 1 else 0 end;
SQL

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260728_lifeswitch_usage_v1_contract.sql"

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory <<'SQL'
select 1 / case when (
  select count(*) from lifeswitch_usage.ai_usage_event_v1
  where helper='openai_chat_completions_v1'
    and idempotency_key=answer_id::text
    and event_schema_version=1
)=2 then 1 else 0 end;
SQL

production_after="$(
  docker exec "$live_container" psql -X -U sage -d memory -At -F '|' -c \
    "select count(*),coalesce(sum(total_tokens),0),min(recorded_at),max(recorded_at) from lifeswitch_usage.ai_usage_event_v1"
)"
if test "$production_before" != "$production_after"; then
  printf 'production usage fingerprint changed during clone validation\n' >&2
  exit 1
fi

printf 'CLONE_MIGRATION_ASSERTIONS apply=pass rollback=pass reapply=pass rls=pass append_only=pass production_unchanged=pass\n'
