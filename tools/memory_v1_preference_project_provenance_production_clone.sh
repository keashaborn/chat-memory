#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_PREFERENCE_PROJECT_PROVENANCE_CLONE_PORT:-55446}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1preferenceprojectprovenanceclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
staging=ops/sql/20260713_memory_v1_preference_project_staging.sql
review=ops/sql/20260713_memory_v1_review_apply.sql
installed_contract=tests/memory_v1_preference_project_installed_contract.sql
backup=$(mktemp /tmp/memory-v1-preference-project-provenance.XXXXXX.dump)
before_rows=$(mktemp /tmp/memory-v1-preference-project-provenance-before.XXXXXX.tsv)
after_rows=$(mktemp /tmp/memory-v1-preference-project-provenance-after.XXXXXX.tsv)
before_schema=$(mktemp /tmp/memory-v1-preference-project-provenance-before-schema.XXXXXX.sql)
after_schema=$(mktemp /tmp/memory-v1-preference-project-provenance-after-schema.XXXXXX.sql)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f \
    "$backup" "$before_rows" "$after_rows" \
    "$before_schema" "$after_schema"
}
trap cleanup EXIT
chmod 0600 \
  "$backup" "$before_rows" "$after_rows" \
  "$before_schema" "$after_schema"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

capture_rows() {
  local output=$1
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(
               digest(
                 coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
                 'sha256'
               ),
               'hex'
             )
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM \"$schema\".\"$table\" AS table_row
      ) rows
    ")
    printf '%s.%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done < <(scalar "
    SELECT table_schema || E'\\t' || table_name
    FROM information_schema.tables
    WHERE table_type='BASE TABLE' AND table_schema='memory'
    ORDER BY table_schema,table_name
  ")
}

capture_schema() {
  local output=$1
  "${compose[@]}" exec -T postgres pg_dump -U sage -d memory \
    --schema-only --no-owner --no-privileges \
    | sed -E \
        -e '/^\\(un)?restrict /d' \
        -e '/^-- Dumped from database version /d' \
        -e '/^-- Dumped by pg_dump version /d' \
    >"$output"
}

for required in "$staging" "$review" "$installed_contract"; do
  [[ -f "$repo_root/$required" ]]
done

if rg -n '(^|[^a-zA-Z])(OpenAI|responses\.create|chat\.completions)' \
  "$repo_root/$staging" "$repo_root/$review" \
  "$repo_root/$installed_contract"; then
  echo "preference/project provenance path contains a model caller" >&2
  exit 1
fi

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]

"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE memory_evidence_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_review_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_intake_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_queue_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_worker_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner <"$backup"

capture_rows "$before_rows"
capture_schema "$before_schema"

run_sql <"$staging"
run_sql <"$staging"
run_sql <"$review"
run_sql <"$review"
run_sql <"$installed_contract"

capture_rows "$after_rows"
capture_schema "$after_schema"
cmp -s "$before_rows" "$after_rows"
if ! cmp -s "$before_schema" "$after_schema"; then
  diff -u "$before_schema" "$after_schema" | sed -n '1,240p' >&2
  echo "preference/project migrations changed the current production schema" >&2
  exit 1
fi

echo "memory_v1_preference_project_provenance_production_clone: PASS"
