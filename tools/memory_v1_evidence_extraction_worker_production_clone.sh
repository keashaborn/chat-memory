#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_EVIDENCE_EXTRACTION_WORKER_CLONE_PORT:-55444}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1evidenceextractionworkerclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
lifecycle_migration=ops/sql/20260713_memory_v1_evidence_lifecycle.sql
writer_migration=ops/sql/20260716_memory_v1_record_evidence_api.sql
privilege_migration=ops/sql/20260716_memory_v1_revoke_direct_evidence_insert.sql
selector_migration=ops/sql/20260716_memory_v1_evidence_intake_selector.sql
queue_migration=ops/sql/20260716_memory_v1_evidence_extraction_queue.sql
migration=ops/sql/20260716_memory_v1_evidence_extraction_worker.sql
rollback=ops/sql/20260716_memory_v1_evidence_extraction_worker_rollback.sql
test_sql=tests/memory_v1_evidence_extraction_worker.sql
backup=$(mktemp /tmp/memory-v1-evidence-extraction-worker.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-evidence-extraction-worker-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-evidence-extraction-worker-after.XXXXXX.tsv)
rolled_back=$(mktemp /tmp/memory-v1-evidence-extraction-worker-rollback.XXXXXX.tsv)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$before" "$after" "$rolled_back"
}
trap cleanup EXIT
chmod 0600 "$backup" "$before" "$after" "$rolled_back"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

capture_logical_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(
               digest(
                 coalesce(
                   string_agg(row_json,E'\\n' ORDER BY row_json),
                   ''
                 ),
                 'sha256'
               ),
               'hex'
             )
      FROM (
        SELECT (
          CASE
            WHEN '$table'='evidence_extraction_job'
              THEN to_jsonb(table_row)
                - 'checkpoint_sequence'
                - 'checkpoint_sha256'
            WHEN '$table'='evidence_extraction_event'
              THEN to_jsonb(table_row)-'operation_id'
            ELSE to_jsonb(table_row)
          END
        )::text AS row_json
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

for required in \
  "$lifecycle_migration" \
  "$writer_migration" \
  "$privilege_migration" \
  "$selector_migration" \
  "$queue_migration" \
  "$migration" \
  "$rollback" \
  "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
done

if rg -n '(^|[^a-zA-Z])(OpenAI|responses\.create|chat\.completions)' \
  "$repo_root/$migration" \
  "$repo_root/$test_sql"; then
  echo "evidence extraction worker database patch contains a model caller" >&2
  exit 1
fi

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
run_sql <"$queue_migration"

[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job")" == "0" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event")" == "0" ]]
capture_logical_state "$before"

run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"

[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job")" == "0" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event")" == "0" ]]
capture_logical_state "$after"
cmp -s "$before" "$after"

run_sql <"$rollback"
[[ "$(scalar "
  SELECT (
    to_regclass('memory.evidence_extraction_job') IS NOT NULL
    AND to_regclass('memory.evidence_extraction_event') IS NOT NULL
    AND to_regprocedure(
      'memory.enqueue_owner_evidence_extraction_v1(uuid,text,text,text,text)'
    ) IS NOT NULL
    AND to_regprocedure(
      'memory.claim_owner_evidence_extraction_job_v1(uuid,text,text,integer,integer)'
    ) IS NULL
    AND to_regprocedure(
      'memory.checkpoint_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,integer,text,jsonb,integer)'
    ) IS NULL
    AND to_regprocedure(
      'memory.finish_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,jsonb)'
    ) IS NULL
    AND to_regprocedure(
      'memory.fail_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,integer)'
    ) IS NULL
    AND to_regprocedure(
      'memory.resolve_owner_evidence_extraction_review_v1(uuid,uuid,text,text,text,jsonb)'
    ) IS NULL
    AND to_regrole('memory_extraction_worker_maintainer') IS NULL
    AND NOT EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_schema='memory'
        AND (
          (
            table_name='evidence_extraction_job'
            AND column_name IN ('checkpoint_sequence','checkpoint_sha256')
          )
          OR
          (
            table_name='evidence_extraction_event'
            AND column_name='operation_id'
          )
        )
    )
  )::integer
")" == "1" ]]
capture_logical_state "$rolled_back"
cmp -s "$before" "$rolled_back"

echo "memory_v1_evidence_extraction_worker_production_clone: PASS"
