#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_RECORD_EVIDENCE_CLONE_PORT:-55440}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1recordevidenceclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
lifecycle_migration=ops/sql/20260713_memory_v1_evidence_lifecycle.sql
migration=ops/sql/20260716_memory_v1_record_evidence_api.sql
rollback=ops/sql/20260716_memory_v1_record_evidence_api_rollback.sql
test_sql=tests/memory_v1_record_evidence_api.sql
integration=tests/memory_v1_record_evidence_api_integration.py
backup=$(mktemp /tmp/memory-v1-record-evidence.XXXXXX.dump)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup"
}
trap cleanup EXIT
chmod 0600 "$backup"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE memory_evidence_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_review_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' 'GRANT USAGE ON SCHEMA memory TO brains_app;' | run_sql
run_sql <"$lifecycle_migration"
run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"

if rg -n "INSERT INTO memory\\.evidence" \
  "$repo_root/rag_engine/memory_v1_store.py"; then
  echo "runtime store still contains a direct evidence INSERT" >&2
  exit 1
fi

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$integration"

[[ "$(scalar "
  SELECT (
    (SELECT count(*) FROM memory.evidence
      WHERE owner_user_id='77777777-7777-4777-8777-777777777777'::uuid)=1
    AND (SELECT count(*) FROM memory.evidence
      WHERE owner_user_id='88888888-8888-4888-8888-888888888888'::uuid)=1
    AND has_function_privilege(
      'brains_app',
      'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)',
      'EXECUTE'
    )
    AND has_table_privilege(
      'memory_evidence_maintainer','memory.evidence','INSERT'
    )
    AND NOT has_table_privilege('brains_app','memory.evidence','UPDATE')
    AND NOT has_table_privilege('brains_app','memory.evidence','DELETE')
  )::int")" == "1" ]]

run_sql <"$rollback"
[[ "$(scalar "
  SELECT (
    to_regprocedure(
      'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)'
    ) IS NULL
    AND NOT has_table_privilege(
      'memory_evidence_maintainer','memory.evidence','INSERT'
    )
    AND (SELECT count(*) FROM memory.evidence
      WHERE owner_user_id IN (
        '77777777-7777-4777-8777-777777777777'::uuid,
        '88888888-8888-4888-8888-888888888888'::uuid
      ))=2
  )::int")" == "1" ]]

echo "memory_v1_record_evidence_api_production_clone: PASS"
