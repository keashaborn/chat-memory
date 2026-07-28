#!/usr/bin/env bash
set -euo pipefail

live_container="brains-postgres-1"
clone_name="lifeswitch-usage-system-lanes-v1-clone"
clone_password="clone-only-usage-system-lanes-v1"
candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
clone_image="$(docker inspect -f '{{.Config.Image}}' "$live_container")"
schema_tmp="$(mktemp /tmp/lifeswitch-usage-system-lanes-schema.XXXXXX.sql)"

cleanup() {
  docker rm -f lifeswitch-usage-system-lanes-v1-clone >/dev/null 2>&1 || true
  rm -f "$schema_tmp"
}
trap cleanup EXIT

if docker container inspect "$clone_name" >/dev/null 2>&1; then
  echo "clone container already exists" >&2
  exit 1
fi

docker exec "$live_container" pg_dump \
  -U sage \
  -d memory \
  --schema-only \
  --no-owner \
  --no-privileges \
  --schema=catalog_dev \
  --schema=lifeswitch_nutrition \
  --schema=lifeswitch_training \
  > "$schema_tmp"

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
  -X -v ON_ERROR_STOP=1 -U sage -d memory < "$schema_tmp"
docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260728_lifeswitch_usage_v1.sql"
docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260728_lifeswitch_usage_v1_contract.sql"
docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260728_lifeswitch_usage_actor_registry_v1.sql"

docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  -c "insert into lifeswitch_usage.ai_usage_event_v1 (
        ai_usage_event_id,owner_user_id,answer_id,provider,operation,helper,
        source_channel,provider_response_id,requested_model,returned_model,
        input_tokens,cached_input_tokens,output_tokens,
        reasoning_output_tokens,total_tokens,idempotency_key,
        event_schema_version,recorded_at
      ) values
      (
        '10000000-0000-4000-8000-000000000001',
        '1240822d-ac9a-4096-95aa-e2b24d36ef50',
        '20000000-0000-4000-8000-000000000001',
        'openai','chat_response','openai_chat_completions_v1','chat',
        'clone-product-response','gpt-5.6-sol','gpt-5.6-sol',
        100,0,20,5,120,'clone-product',1,now()
      ),
      (
        '10000000-0000-4000-8000-000000000002',
        '1b8daceb-e78d-47a6-bfc0-2d6b8e24b33f',
        '20000000-0000-4000-8000-000000000002',
        'openai','chat_response','openai_chat_completions_v1','voice',
        'clone-system-response','gpt-5.6-sol','gpt-5.6-sol',
        50,0,10,2,60,'clone-system',1,now()
      )"

rls_flags="$(
  docker exec "$clone_name" psql -X -U sage -d memory -At -F '|' \
    -c "select relrowsecurity,relforcerowsecurity
        from pg_class
        where oid='lifeswitch_usage.ai_actor_registry_v1'::regclass"
)"
test "$rls_flags" = "t|t"

if docker exec "$clone_name" psql -X -U sage -d memory \
  -c "update lifeswitch_usage.ai_actor_registry_v1
      set display_label='mutated'" >/dev/null 2>&1; then
  echo "registry update unexpectedly succeeded" >&2
  exit 1
fi

clone_port="$(
  docker inspect -f \
    '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}' \
    "$clone_name"
)"
clone_dsn="postgresql://sage:${clone_password}@127.0.0.1:${clone_port}/memory"

PYTHONPATH="$candidate" /opt/chat-memory/venv/bin/python -c "
import asyncio
from rag_engine.usage_ledger_v1 import (
    AdminUsageUsersRequestV1,
    build_admin_usage_overview_v1,
    build_admin_usage_users_v1,
)
overview = asyncio.run(
    build_admin_usage_overview_v1(dsn='${clone_dsn}', window_days=30)
)
users = asyncio.run(
    build_admin_usage_users_v1(
        dsn='${clone_dsn}',
        request=AdminUsageUsersRequestV1(window_days=30, limit=25),
        cursor_secret='clone-cursor-secret',
    )
)
assert overview['active_users'] == 1
assert overview['ai']['requests'] == 1
assert overview['ai']['total_tokens'] == 120
assert overview['system_ai']['requests'] == 1
assert overview['system_ai']['total_tokens'] == 60
assert overview['system_ai']['workloads'][0]['workload_key'] == (
    'voice_synthetic_canary'
)
assert len(users['items']) == 1
assert users['items'][0]['user_id'] == (
    '1240822d-ac9a-4096-95aa-e2b24d36ef50'
)
print('CLONE_CONTRACT=product-1-120 system-1-60 users-1')
"

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260728_lifeswitch_usage_actor_registry_v1_rollback.sql"

rollback_state="$(
  docker exec "$clone_name" psql -X -U sage -d memory -At -F '|' \
    -c "select
          to_regclass('lifeswitch_usage.ai_actor_registry_v1') is null,
          count(*)
        from lifeswitch_usage.ai_usage_event_v1"
)"
test "$rollback_state" = "t|2"

printf '%s\n' \
  "CLONE_ROLLBACK=registry-absent raw-events-preserved-2"
