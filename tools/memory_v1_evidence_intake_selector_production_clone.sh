#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_EVIDENCE_INTAKE_CLONE_PORT:-55442}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1evidenceintakeclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
lifecycle_migration=ops/sql/20260713_memory_v1_evidence_lifecycle.sql
writer_migration=ops/sql/20260716_memory_v1_record_evidence_api.sql
privilege_migration=ops/sql/20260716_memory_v1_revoke_direct_evidence_insert.sql
migration=ops/sql/20260716_memory_v1_evidence_intake_selector.sql
rollback=ops/sql/20260716_memory_v1_evidence_intake_selector_rollback.sql
test_sql=tests/memory_v1_evidence_intake_selector.sql
selector=scripts/memory_v1_evidence_intake_selector.py
backup=$(mktemp /tmp/memory-v1-evidence-intake.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-evidence-intake-before.XXXXXX.tsv)
after_dry=$(mktemp /tmp/memory-v1-evidence-intake-after-dry.XXXXXX.tsv)
after_record=$(mktemp /tmp/memory-v1-evidence-intake-after-record.XXXXXX.tsv)
terminal_before_replay=$(mktemp /tmp/memory-v1-evidence-intake-terminal.XXXXXX.tsv)
terminal_after_replay=$(mktemp /tmp/memory-v1-evidence-intake-terminal.XXXXXX.tsv)
dry_report=$(mktemp /tmp/memory-v1-evidence-intake-dry.XXXXXX.json)
record_report=$(mktemp /tmp/memory-v1-evidence-intake-record.XXXXXX.json)
replay_report=$(mktemp /tmp/memory-v1-evidence-intake-replay.XXXXXX.json)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
owner_lifeswitch=557ea042-cb82-48f8-9429-472e96c957ef
owner_doctor=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f \
    "$backup" "$before" "$after_dry" "$after_record" \
    "$terminal_before_replay" "$terminal_after_replay" \
    "$dry_report" "$record_report" "$replay_report"
}
trap cleanup EXIT
chmod 0600 \
  "$backup" "$before" "$after_dry" "$after_record" \
  "$terminal_before_replay" "$terminal_after_replay" \
  "$dry_report" "$record_report" "$replay_report"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

capture_nonterminal_state() {
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
    WHERE table_schema='memory'
      AND table_type='BASE TABLE'
      AND table_name<>'evidence_intake_terminal'
    ORDER BY table_name
  ")
}

capture_terminal_state() {
  local output=$1
  scalar "
    SELECT count(*)::text || E'\\t' ||
           encode(digest(coalesce(string_agg(row_json,E'\\n'
             ORDER BY row_json),''),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(table_row)::text AS row_json
      FROM memory.evidence_intake_terminal AS table_row
    ) rows
  " >"$output"
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
run_sql <"$privilege_migration"
run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"

if rg -n '(^|[^a-zA-Z])(OpenAI|responses\.create|chat\.completions)' \
  "$repo_root/$selector"; then
  echo "evidence intake selector contains an external model caller" >&2
  exit 1
fi

capture_nonterminal_state "$before"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$selector" \
  --owner-user-id "$owner_lifeswitch" \
  --owner-user-id "$owner_doctor" \
  --selector-version 20260716_v1 \
  --limit 100 \
  --report-path "$dry_report"
[[ "$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal")" == "0" ]]
capture_nonterminal_state "$after_dry"
cmp -s "$before" "$after_dry"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$selector" \
  --owner-user-id "$owner_lifeswitch" \
  --owner-user-id "$owner_doctor" \
  --selector-version 20260716_v1 \
  --limit 100 \
  --record-terminal \
  --report-path "$record_report"
capture_nonterminal_state "$after_record"
cmp -s "$before" "$after_record"

[[ "$(scalar "
  SELECT (
    count(*)=26
    AND count(*) FILTER(WHERE outcome='empty')=24
    AND count(*) FILTER(WHERE outcome='skipped')=2
    AND count(*) FILTER(
      WHERE owner_user_id='$owner_lifeswitch'::uuid
    )=20
    AND count(*) FILTER(
      WHERE owner_user_id='$owner_doctor'::uuid
    )=6
  )::int
  FROM memory.evidence_intake_terminal
  WHERE selector_version='20260716_v1'
")" == "1" ]]

capture_terminal_state "$terminal_before_replay"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$selector" \
  --owner-user-id "$owner_lifeswitch" \
  --owner-user-id "$owner_doctor" \
  --selector-version 20260716_v1 \
  --limit 100 \
  --record-terminal \
  --report-path "$replay_report"
capture_terminal_state "$terminal_after_replay"
cmp -s "$terminal_before_replay" "$terminal_after_replay"

[[ "$(scalar "
  SELECT (
    NOT has_table_privilege(
      'brains_app','memory.evidence_intake_terminal','INSERT'
    )
    AND NOT has_table_privilege(
      'brains_app','memory.evidence_intake_terminal','UPDATE'
    )
    AND NOT has_table_privilege(
      'brains_app','memory.evidence_intake_terminal','DELETE'
    )
    AND has_table_privilege(
      'brains_app','memory.evidence_intake_terminal','SELECT'
    )
  )::int
")" == "1" ]]

run_sql <"$rollback"
[[ "$(scalar "
  SELECT (
    to_regclass('memory.evidence_intake_terminal') IS NULL
    AND to_regprocedure(
      'memory.plan_owner_evidence_intake_v1(text,integer,uuid)'
    ) IS NULL
    AND to_regprocedure(
      'memory.record_owner_evidence_intake_terminal_v1(uuid,text,text,text,text)'
    ) IS NULL
    AND to_regrole('memory_intake_maintainer') IS NULL
  )::int
")" == "1" ]]

echo "memory_v1_evidence_intake_selector_production_clone: PASS"
