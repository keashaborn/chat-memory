#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_CHAT_CAPTURE_CLONE_PORT:-55450}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5chatcaptureclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
lifecycle_migration=ops/sql/20260713_memory_v1_evidence_lifecycle.sql
writer_migration=ops/sql/20260716_memory_v1_record_evidence_api.sql
privilege_migration=ops/sql/20260716_memory_v1_revoke_direct_evidence_insert.sql
selector_migration=ops/sql/20260716_memory_v1_evidence_intake_selector.sql
compat_migration=ops/sql/20260717_memory_v1_v5_chat_capture_compat.sql
compat_rollback=ops/sql/20260717_memory_v1_v5_chat_capture_compat_rollback.sql
queue_migration=ops/sql/20260716_memory_v1_evidence_extraction_queue.sql
fixture_sql=tests/memory_v1_v5_chat_capture_fixture.sql
assert_sql=tests/memory_v1_v5_chat_capture_assert.sql
capture=scripts/memory_v1_v5_chat_capture.py
dispatcher=scripts/memory_v1_evidence_intake_dispatcher.py
backup=$(mktemp /tmp/memory-v1-v5-chat-capture.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-v5-chat-capture-before.XXXXXX.tsv)
after_dry=$(mktemp /tmp/memory-v1-v5-chat-capture-after-dry.XXXXXX.tsv)
after_apply=$(mktemp /tmp/memory-v1-v5-chat-capture-after-apply.XXXXXX.tsv)
before_replay=$(mktemp /tmp/memory-v1-v5-chat-capture-before-replay.XXXXXX.tsv)
after_replay=$(mktemp /tmp/memory-v1-v5-chat-capture-after-replay.XXXXXX.tsv)
before_dispatch=$(mktemp /tmp/memory-v1-v5-chat-capture-before-dispatch.XXXXXX.tsv)
after_dispatch=$(mktemp /tmp/memory-v1-v5-chat-capture-after-dispatch.XXXXXX.tsv)
before_dispatch_replay=$(mktemp /tmp/memory-v1-v5-chat-capture-dispatch-replay-before.XXXXXX.tsv)
after_dispatch_replay=$(mktemp /tmp/memory-v1-v5-chat-capture-dispatch-replay-after.XXXXXX.tsv)
dry_report=$(mktemp /tmp/memory-v1-v5-chat-capture-dry.XXXXXX.json)
apply_report=$(mktemp /tmp/memory-v1-v5-chat-capture-apply.XXXXXX.json)
replay_report=$(mktemp /tmp/memory-v1-v5-chat-capture-replay.XXXXXX.json)
dispatch_report=$(mktemp /tmp/memory-v1-v5-chat-capture-dispatch.XXXXXX.json)
dispatch_replay_report=$(mktemp /tmp/memory-v1-v5-chat-capture-dispatch-replay.XXXXXX.json)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
owner_a=a1111111-1111-4111-8111-111111111111
owner_b=b2222222-2222-4222-8222-222222222222

temporary_files=(
  "$backup" "$before" "$after_dry" "$after_apply"
  "$before_replay" "$after_replay" "$before_dispatch" "$after_dispatch"
  "$before_dispatch_replay" "$after_dispatch_replay"
  "$dry_report" "$apply_report" "$replay_report"
  "$dispatch_report" "$dispatch_replay_report"
)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "${temporary_files[@]}"
}
trap cleanup EXIT
chmod 0600 "${temporary_files[@]}"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

capture_memory_state() {
  local output=$1
  local exclusions=$2
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
      AND table_name NOT IN ($exclusions)
    ORDER BY table_name
  ")
}

chat_signature() {
  scalar "
    SELECT count(*)::text || E'\\t' ||
           encode(digest(coalesce(string_agg(row_json,E'\\n'
             ORDER BY row_json),''),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(log)::text AS row_json
      FROM public.chat_log AS log
      WHERE log.id IN (
        '11111111-1111-4111-8111-111111111101',
        '11111111-1111-4111-8111-111111111102',
        '11111111-1111-4111-8111-111111111103',
        '22222222-2222-4222-8222-222222222201'
      )
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
  'CREATE ROLE memory_intake_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_queue_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_trace_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' \
  'GRANT USAGE ON SCHEMA public,memory TO brains_app;' \
  'GRANT SELECT ON public.chat_log TO brains_app;' \
  | run_sql
run_sql <"$lifecycle_migration"
run_sql <"$writer_migration"
run_sql <"$privilege_migration"
run_sql <"$selector_migration"
run_sql <"$compat_migration"
run_sql <"$compat_migration"
run_sql <"$queue_migration"
run_sql <"$queue_migration"
run_sql <"$fixture_sql"

if rg -n 'from openai|import openai|responses\.create|chat\.completions|QdrantClient|\.upsert\(' \
  "$repo_root/$capture"; then
  echo "V5 chat capture contains an external or vector writer" >&2
  exit 1
fi

evidence_before=$(scalar "SELECT count(*) FROM memory.evidence")
chat_before=$(chat_signature)
capture_memory_state "$before" "'evidence'"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$capture" \
  --owner-user-id "$owner_a" --owner-user-id "$owner_b" \
  --limit 100 --report-path "$dry_report"
capture_memory_state "$after_dry" "'evidence'"
cmp -s "$before" "$after_dry"
[[ "$(scalar "SELECT count(*) FROM memory.evidence")" == "$evidence_before" ]]
[[ "$(chat_signature)" == "$chat_before" ]]

MEMORY_V1_V5_CHAT_CAPTURE_APPLY=enabled POSTGRES_DSN="$dsn" \
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$capture" \
  --owner-user-id "$owner_a" --owner-user-id "$owner_b" \
  --limit 100 --apply --report-path "$apply_report"
capture_memory_state "$after_apply" "'evidence'"
cmp -s "$before" "$after_apply"
[[ "$(scalar "SELECT count(*) FROM memory.evidence")" == "$((evidence_before + 3))" ]]
[[ "$(chat_signature)" == "$chat_before" ]]
if rg -q 'Synthetic project|Synthetic ordinary|Synthetic second-owner' \
  "$dry_report" "$apply_report"; then
  echo "capture report leaked source text" >&2
  exit 1
fi

capture_memory_state "$before_replay" "''"
MEMORY_V1_V5_CHAT_CAPTURE_APPLY=enabled POSTGRES_DSN="$dsn" \
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$capture" \
  --owner-user-id "$owner_a" --owner-user-id "$owner_b" \
  --limit 100 --apply --report-path "$replay_report"
capture_memory_state "$after_replay" "''"
cmp -s "$before_replay" "$after_replay"
[[ "$(rg -c '\"planned_count\": 0' "$replay_report")" == "2" ]]

run_sql <"$assert_sql"

terminal_before=$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal")
job_before=$(scalar "SELECT count(*) FROM memory.evidence_extraction_job")
event_before=$(scalar "SELECT count(*) FROM memory.evidence_extraction_event")
capture_memory_state "$before_dispatch" \
  "'evidence_intake_terminal','evidence_extraction_job','evidence_extraction_event'"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$dispatcher" \
  --owner-user-id "$owner_a" --owner-user-id "$owner_b" \
  --selector-version 20260717_v2_fixture --limit 100 --apply \
  --report-path "$dispatch_report"
capture_memory_state "$after_dispatch" \
  "'evidence_intake_terminal','evidence_extraction_job','evidence_extraction_event'"
cmp -s "$before_dispatch" "$after_dispatch"
[[ "$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal")" == "$((terminal_before + 3))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job")" == "$((job_before + 3))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event")" == "$((event_before + 3))" ]]

capture_memory_state "$before_dispatch_replay" "''"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$dispatcher" \
  --owner-user-id "$owner_a" --owner-user-id "$owner_b" \
  --selector-version 20260717_v2_fixture --limit 100 --apply \
  --report-path "$dispatch_replay_report"
capture_memory_state "$after_dispatch_replay" "''"
cmp -s "$before_dispatch_replay" "$after_dispatch_replay"

run_sql <"$compat_rollback"
[[ "$(scalar "SELECT (pg_get_functiondef('memory.plan_owner_evidence_intake_v1(text,integer,uuid)'::regprocedure) NOT LIKE '%20260717_v2%')::int")" == "1" ]]

echo "memory_v1_v5_chat_capture_production_clone: PASS"
