#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into an isolated PostgreSQL clone,
# validates the aggregate pipeline reader and bounded entailment worker, then
# removes the clone. It performs no production database or Qdrant writes.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
port=${MEMORY_V1_PIPELINE_STATUS_CLONE_PORT:-55479}
project=memoryv1pipelinestatusclone
compose=(docker compose -p "$project" -f docker-compose.ci.yml \
  -f docker-compose.stage-batch-clone.yml)
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=673d64a3-c4ba-4d1c-89e3-e0c579022fad
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
migration=ops/sql/20260730_memory_v1_admin_pipeline_status_v1.sql
rollback=ops/sql/20260730_memory_v1_admin_pipeline_status_v1_rollback.sql
sql_test=tests/memory_v1_admin_pipeline_status_v1.sql
worker=scripts/memory_v1_v5_local_entailment_scheduler.py

backup=$(mktemp /tmp/memory-v1-pipeline-status.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v1-pipeline-status.XXXXXX)
roles="$work/roles.sql"
tables="$work/tables.tsv"
before="$work/before.tsv"
after="$work/after.tsv"
owner_dry="$work/owner.json"
other_dry="$work/other.json"

cleanup() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" down -v \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup"

run_sql() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
    psql -X -v ON_ERROR_STOP=1 -U sage -d memory "$@"
}

scalar() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory -c "$1" \
    | tr -d '[:space:]'
}

capture_rows() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text || ':' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
        ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value) rows")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$tables"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for required in "$migration" "$rollback" "$sql_test" "$worker"; do
  [[ -f "$required" ]]
done
git diff --check
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python -m unittest \
  tests.test_admin_memory_health_v1 \
  tests.test_memory_v1_v5_local_entailment_scheduler
python3 -m py_compile "$worker" rag_engine/admin_memory_health_v1.py
systemd-analyze verify \
  ops/systemd/memory-v1-v5-local-entailment.service \
  ops/systemd/memory-v1-v5-local-entailment.timer

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c "
  SELECT format(
    'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;',
    rolname
  )
  FROM pg_roles
  WHERE rolname NOT LIKE 'pg\\_%'
    AND rolname NOT IN ('sage','brains_app')
  ORDER BY rolname
" >"$roles"

MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  | run_sql >/dev/null
run_sql <"$roles" >/dev/null
MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
  pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' 'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  | run_sql >/dev/null

run_sql -At -c "SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE'
    AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name" >"$tables"
capture_rows "$before"
qdrant_before=$(qdrant_signature)

run_sql <"$migration" >/dev/null
run_sql <"$migration" >/dev/null
run_sql <"$sql_test" >/dev/null

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --max-records 10 >"$owner_dry"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$other" --max-records 10 >"$other_dry"
for dry in "$owner_dry" "$other_dry"; do
  jq -e '
    .apply == false
    and .max_records == 10
    and .selected_records <= 10
    and .database_writes == 0
    and .local_model_calls == 0
    and .external_model_calls == 0
    and .qdrant_writes == 0
    and .claim_writes == 0
    and .projection_writes == 0
    and .prompt_influence == 0
  ' "$dry" >/dev/null
done

capture_rows "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

run_sql <"$rollback" >/dev/null
[[ "$(scalar "SELECT to_regprocedure(
  'memory.read_owner_pipeline_status_v1()'
) IS NULL")" == t ]]
[[ "$(scalar "SELECT to_regrole(
  'memory_pipeline_status_reader_v1'
) IS NULL")" == t ]]

printf '%s\n' 'memory_v1_bounded_pipeline_status_clone: PASS'
