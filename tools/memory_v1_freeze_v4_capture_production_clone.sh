#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_FREEZE_V4_CAPTURE_CLONE_PORT:-55445}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1freezev4captureclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260716_memory_v1_freeze_v4_capture.sql
rollback=ops/sql/20260716_memory_v1_freeze_v4_capture_rollback.sql
test_sql=tests/memory_v1_freeze_v4_capture.sql
backup=$(mktemp /tmp/memory-v1-freeze-v4-capture.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-freeze-v4-capture-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-freeze-v4-capture-after.XXXXXX.tsv)
rolled_back=$(mktemp /tmp/memory-v1-freeze-v4-capture-rollback.XXXXXX.tsv)

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

capture_state() {
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
    WHERE table_type='BASE TABLE'
      AND (
        table_schema='memory'
        OR (table_schema='public' AND table_name='chat_log')
      )
    ORDER BY table_schema,table_name
  ")
}

for required in "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
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
  'CREATE ROLE memory_extraction_queue_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_worker_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"

source_backup_trigger_state=$(scalar "
  SELECT tgenabled
  FROM pg_trigger
  WHERE tgrelid='public.chat_log'::regclass
    AND tgname='chat_log_enqueue_memory_v1_consolidation'
    AND NOT tgisinternal
")
case "$source_backup_trigger_state" in
  O)
    ;;
  D)
    # Production is expected to be frozen after Phase 0 activation. Restore
    # the enabled baseline only inside this disposable clone so both forward
    # and rollback behavior remain testable.
    run_sql <"$rollback"
    ;;
  *)
    echo "unexpected source backup trigger state: $source_backup_trigger_state" >&2
    exit 1
    ;;
esac
[[ "$(scalar "SELECT tgenabled FROM pg_trigger WHERE tgrelid='public.chat_log'::regclass AND tgname='chat_log_enqueue_memory_v1_consolidation' AND NOT tgisinternal")" == "O" ]]
capture_state "$before"

run_sql <"$migration"
run_sql <"$migration"
[[ "$(scalar "SELECT tgenabled FROM pg_trigger WHERE tgrelid='public.chat_log'::regclass AND tgname='chat_log_enqueue_memory_v1_consolidation' AND NOT tgisinternal")" == "D" ]]
run_sql <"$test_sql"
capture_state "$after"
cmp -s "$before" "$after"

run_sql <"$rollback"
[[ "$(scalar "SELECT tgenabled FROM pg_trigger WHERE tgrelid='public.chat_log'::regclass AND tgname='chat_log_enqueue_memory_v1_consolidation' AND NOT tgisinternal")" == "O" ]]

run_sql <<'SQL'
BEGIN;
DO $test$
DECLARE
  synthetic_chat_id constant uuid :=
    'f5555555-5555-4555-8555-555555555555'::uuid;
  synthetic_owner constant uuid :=
    '1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid;
BEGIN
  INSERT INTO public.chat_log(id,user_id,source,text,owner_user_id)
  VALUES (
    synthetic_chat_id,synthetic_owner::text,'frontend/chat:user',
    'Synthetic rollback-only Phase 0 V4 rollback test.',synthetic_owner
  );
  IF NOT EXISTS (
    SELECT 1
    FROM memory.consolidation_job
    WHERE owner_user_id=synthetic_owner
      AND source_system='public.chat_log'
      AND source_external_id=synthetic_chat_id::text
      AND pipeline_version='20260714_v4'
      AND status='pending'
  ) THEN
    RAISE EXCEPTION 'rollback did not restore V4 trigger behavior';
  END IF;
END
$test$;
ROLLBACK;
SQL

capture_state "$rolled_back"
cmp -s "$before" "$rolled_back"

echo "memory_v1_freeze_v4_capture_production_clone: PASS"
echo "source_backup_trigger_state=$source_backup_trigger_state"
