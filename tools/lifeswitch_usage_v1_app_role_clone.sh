#!/usr/bin/env bash
set -euo pipefail

live_container="brains-postgres-1"
candidate="/tmp/chat-memory-admin-usage-v1-release"
clone_name="lifeswitch-usage-app-role-clone-${$}"
clone_password="clone-only-usage-app-role-v1"
clone_image="$(docker inspect -f '{{.Config.Image}}' "$live_container")"
clone_tmp="$(mktemp -d /tmp/lifeswitch-usage-app-role-clone.XXXXXX)"
schema_dump="$clone_tmp/relevant-schema.sql"

cleanup() {
  docker rm -f "$clone_name" >/dev/null 2>&1 || true
  rm -rf "$clone_tmp"
}
trap cleanup EXIT

production_before="$(
  docker exec "$live_container" psql -X -U sage -d memory -At -F '|' -c \
    "select count(*),coalesce(sum(total_tokens),0) from lifeswitch_usage.ai_usage_event_v1"
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
      create extension if not exists unaccent with schema public;
      create role brains_app
        login
        nosuperuser
        nobypassrls
        password '$clone_password';
      create role lifeswitch_usage_writer_v1
        nologin
        nosuperuser
        nobypassrls;
      create role lifeswitch_usage_admin_v1
        nologin
        nosuperuser
        nobypassrls"

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory < "$schema_dump"

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory <<'SQL'
grant usage on schema lifeswitch_usage
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant execute on function lifeswitch_usage.current_actor_user_id()
  to lifeswitch_usage_writer_v1;
grant select,insert on lifeswitch_usage.ai_usage_event_v1
  to lifeswitch_usage_writer_v1;
grant select on lifeswitch_usage.ai_usage_event_v1
  to lifeswitch_usage_admin_v1;
grant usage on schema lifeswitch_nutrition
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant select (
  nutrition_day_id,owner_user_id,day,completed_at
) on lifeswitch_nutrition.nutrition_day
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant select (
  nutrition_entry_id,nutrition_day_id
) on lifeswitch_nutrition.nutrition_entry
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant usage on schema lifeswitch_training
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant select (
  training_session_id,owner_user_id,day,finished_at,is_active
) on lifeswitch_training.training_session_current_v
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant select (
  training_session_id,owner_user_id,is_active
) on lifeswitch_training.training_set_log
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
grant select (
  owner_user_id,day,is_active
) on lifeswitch_training.conditioning_session_current_v
  to lifeswitch_usage_writer_v1,lifeswitch_usage_admin_v1;
SQL

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260728_lifeswitch_usage_v1_app_role_grants.sql"

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory <<'SQL'
select 1 / case when
  pg_has_role(
    'brains_app',
    'lifeswitch_usage_writer_v1',
    'member'
  )
  and pg_has_role(
    'brains_app',
    'lifeswitch_usage_admin_v1',
    'member'
  )
then 1 else 0 end;
select 1 / case when (
  select relrowsecurity and relforcerowsecurity
  from pg_class c
  join pg_namespace n on n.oid=c.relnamespace
  where n.nspname='lifeswitch_usage'
    and c.relname='ai_usage_event_v1'
) then 1 else 0 end;
select 1 / case when not has_column_privilege(
  'lifeswitch_usage_admin_v1',
  'lifeswitch_nutrition.nutrition_day',
  'notes',
  'select'
) then 1 else 0 end;
SQL

clone_port="$(
  docker port "$clone_name" 5432/tcp | sed -n '1s/.*://p'
)"
clone_dsn="postgresql://brains_app:${clone_password}@127.0.0.1:${clone_port}/memory"

POSTGRES_DSN="$clone_dsn" \
PYTHONPATH="$candidate" \
/opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import os
from uuid import UUID

from rag_engine.usage_ledger_v1 import (
    AdminUsageUsersRequestV1,
    build_admin_usage_overview_v1,
    build_admin_usage_user_detail_v1,
    build_admin_usage_users_v1,
)


async def main() -> None:
    dsn = os.environ["POSTGRES_DSN"]
    overview = await build_admin_usage_overview_v1(
        dsn=dsn,
        window_days=30,
    )
    assert overview["active_users"] == 0, overview
    assert overview["ai"]["requests"] == 0, overview

    users = await build_admin_usage_users_v1(
        dsn=dsn,
        request=AdminUsageUsersRequestV1(limit=25),
        cursor_secret="clone-cursor-secret-that-is-long-enough",
    )
    assert users["items"] == [], users
    assert users["has_more"] is False, users

    detail = await build_admin_usage_user_detail_v1(
        dsn=dsn,
        target_user_id=UUID("90000000-0000-4000-8000-000000000001"),
        window_days=30,
    )
    assert detail["summary"]["ai"]["total_tokens"] == 0, detail
    assert detail["daily"] == [], detail

    print(
        "APP_ROLE_CLONE_QUERY_ASSERTIONS "
        "overview=pass users=pass detail=pass"
    )


asyncio.run(main())
PY

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260728_lifeswitch_usage_v1_app_role_grants_rollback.sql"

if docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U brains_app -d memory \
  -c "begin; set local role lifeswitch_usage_admin_v1; rollback" \
  >/dev/null 2>&1
then
  printf 'brains_app retained usage role after rollback\n' >&2
  exit 1
fi

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260728_lifeswitch_usage_v1_app_role_grants.sql"

docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U brains_app -d memory \
  -c "begin;
      set local role lifeswitch_usage_admin_v1;
      select count(*) from lifeswitch_usage.ai_usage_event_v1;
      rollback" >/dev/null

production_after="$(
  docker exec "$live_container" psql -X -U sage -d memory -At -F '|' -c \
    "select count(*),coalesce(sum(total_tokens),0) from lifeswitch_usage.ai_usage_event_v1"
)"
if test "$production_before" != "$production_after"; then
  printf 'production usage fingerprint changed during clone validation\n' >&2
  exit 1
fi

printf 'APP_ROLE_CLONE_MIGRATION_ASSERTIONS apply=pass rollback=pass reapply=pass rls=pass production_unchanged=pass\n'
