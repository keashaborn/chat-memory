#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_EVIDENCE_EXTRACTION_QUEUE_CLONE_PORT:-55443}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1evidenceextractionqueueclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
lifecycle_migration=ops/sql/20260713_memory_v1_evidence_lifecycle.sql
writer_migration=ops/sql/20260716_memory_v1_record_evidence_api.sql
privilege_migration=ops/sql/20260716_memory_v1_revoke_direct_evidence_insert.sql
selector_migration=ops/sql/20260716_memory_v1_evidence_intake_selector.sql
migration=ops/sql/20260716_memory_v1_evidence_extraction_queue.sql
rollback=ops/sql/20260716_memory_v1_evidence_extraction_queue_rollback.sql
test_sql=tests/memory_v1_evidence_extraction_queue.sql
dispatcher=scripts/memory_v1_evidence_intake_dispatcher.py
backup=$(mktemp /tmp/memory-v1-evidence-extraction-queue.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-evidence-extraction-queue-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-evidence-extraction-queue-after.XXXXXX.tsv)
dry_report=$(mktemp /tmp/memory-v1-evidence-extraction-queue-dry.XXXXXX.json)
apply_report=$(mktemp /tmp/memory-v1-evidence-extraction-queue-apply.XXXXXX.json)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
owners=(
  1240822d-ac9a-4096-95aa-e2b24d36ef50
  557ea042-cb82-48f8-9429-472e96c957ef
  d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
  818b60b9-89bd-442a-998c-fc1924184dfc
  5c9f624a-a66d-4183-babb-b3a0f0f4e733
  673d64a3-c4ba-4d1c-89e3-e0c579022fad
)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$before" "$after" "$dry_report" "$apply_report"
}
trap cleanup EXIT
chmod 0600 "$backup" "$before" "$after" "$dry_report" "$apply_report"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

capture_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
}

owner_args=()
for owner in "${owners[@]}"; do
  owner_args+=(--owner-user-id "$owner")
done

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
  'CREATE ROLE memory_intake_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' 'GRANT USAGE ON SCHEMA memory TO brains_app;' | run_sql
run_sql <"$lifecycle_migration"
run_sql <"$writer_migration"
run_sql <"$privilege_migration"
run_sql <"$selector_migration"
run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"

if rg -n '(^|[^a-zA-Z])(OpenAI|responses\.create|chat\.completions)' \
  "$repo_root/$dispatcher"; then
  echo "evidence intake dispatcher contains an external model caller" >&2
  exit 1
fi

[[ "$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal")" == "26" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job")" == "0" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event")" == "0" ]]
capture_state "$before"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$dispatcher" \
  "${owner_args[@]}" \
  --selector-version 20260716_v1 \
  --limit 100 \
  --report-path "$dry_report"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$dispatcher" \
  "${owner_args[@]}" \
  --selector-version 20260716_v1 \
  --limit 100 \
  --apply \
  --report-path "$apply_report"

[[ "$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal")" == "26" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job")" == "0" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event")" == "0" ]]
capture_state "$after"
cmp -s "$before" "$after"

run_sql <"$rollback"
[[ "$(scalar "
  SELECT (
    to_regclass('memory.evidence_extraction_job') IS NULL
    AND to_regclass('memory.evidence_extraction_event') IS NULL
    AND to_regprocedure(
      'memory.enqueue_owner_evidence_extraction_v1(uuid,text,text,text,text)'
    ) IS NULL
    AND to_regrole('memory_extraction_queue_maintainer') IS NULL
    AND pg_get_constraintdef(
      (
        SELECT oid
        FROM pg_constraint
        WHERE conrelid='memory.evidence_intake_terminal'::regclass
          AND conname='evidence_intake_terminal_outcome_check'
      )
    ) NOT LIKE '%dispatched%'
  )::int
")" == "1" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal")" == "26" ]]

echo "memory_v1_evidence_extraction_queue_production_clone: PASS"
