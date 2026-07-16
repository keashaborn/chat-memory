#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_EVIDENCE_PRIVILEGE_CLONE_PORT:-55441}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1evidenceprivilegeclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
lifecycle_migration=ops/sql/20260713_memory_v1_evidence_lifecycle.sql
writer_migration=ops/sql/20260716_memory_v1_record_evidence_api.sql
migration=ops/sql/20260716_memory_v1_revoke_direct_evidence_insert.sql
rollback=ops/sql/20260716_memory_v1_revoke_direct_evidence_insert_rollback.sql
test_sql=tests/memory_v1_revoke_direct_evidence_insert.sql
integration=tests/memory_v1_record_evidence_api_integration.py
backup=$(mktemp /tmp/memory-v1-evidence-privilege.XXXXXX.dump)
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

evidence_signature() {
  scalar "
    SELECT count(*)::text || E'\\t' ||
           encode(digest(coalesce(string_agg(row_json,E'\\n'
             ORDER BY row_json),''),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(evidence_row)::text AS row_json
      FROM memory.evidence AS evidence_row
    ) rows
  "
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
run_sql <"$writer_migration"
run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"

if rg -n -U --glob '*.py' \
  'INSERT\s+INTO\s+memory\.evidence\s*\(' \
  "$repo_root/rag_engine" "$repo_root/scripts"; then
  echo "Python runtime still contains a direct evidence INSERT" >&2
  exit 1
fi

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$integration"
replay_before=$(evidence_signature)
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$integration"
replay_after=$(evidence_signature)
[[ "$replay_after" == "$replay_before" ]]

[[ "$(scalar "
  SELECT (
    has_table_privilege('brains_app','memory.evidence','SELECT')
    AND NOT has_table_privilege('brains_app','memory.evidence','INSERT')
    AND NOT has_table_privilege('brains_app','memory.evidence','UPDATE')
    AND NOT has_table_privilege('brains_app','memory.evidence','DELETE')
    AND has_table_privilege(
      'memory_evidence_maintainer','memory.evidence','INSERT'
    )
    AND has_function_privilege(
      'brains_app',
      'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)',
      'EXECUTE'
    )
  )::int")" == "1" ]]

run_sql <"$rollback"
[[ "$(scalar "
  SELECT (
    has_table_privilege('brains_app','memory.evidence','SELECT')
    AND has_table_privilege('brains_app','memory.evidence','INSERT')
    AND NOT has_table_privilege('brains_app','memory.evidence','UPDATE')
    AND NOT has_table_privilege('brains_app','memory.evidence','DELETE')
    AND has_table_privilege(
      'memory_evidence_maintainer','memory.evidence','INSERT'
    )
  )::int")" == "1" ]]

echo "memory_v1_revoke_direct_evidence_insert_production_clone: PASS"
