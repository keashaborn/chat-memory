#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -p memoryv1v5selfroleprodclone -f docker-compose.ci.yml)
migration=ops/sql/20260716_memory_v1_self_role_compat_v5.sql
rollback=ops/sql/20260716_memory_v1_self_role_compat_v5_rollback.sql
test_sql=tests/memory_v1_self_role_compat_v5.sql

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U sage -d memory
}

"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  'CREATE ROLE brains_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  --schema-only --no-owner --no-privileges | run_sql

run_sql < "$migration"
run_sql < "$migration"
run_sql < "$test_sql"
"${compose[@]}" exec -T postgres pg_dump -U sage -d memory --schema-only \
  --schema=memory --no-owner --no-privileges >/dev/null
run_sql < "$rollback"

definition=$("${compose[@]}" exec -T postgres psql -X -A -t \
  -U sage -d memory -c \
  "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='memory.entity_mention'::regclass AND conname='entity_mention_check'")
if [[ "$definition" != *"relationship_role IS NULL"* \
   || "$definition" == *"user:self"* ]]; then
  echo 'self-role compatibility rollback did not restore strict constraint' >&2
  exit 1
fi

echo 'memory_v1_self_role_compat_v5_production_clone: PASS'
