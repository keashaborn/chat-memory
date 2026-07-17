#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the append-only sanitized V5 shadow-trace sink.
# The rollback probe writes only inside a transaction that is explicitly rolled back.

if [[ "${MEMORY_V1_V5_SHADOW_TRACE_INSTALL:-}" != "authorized" ]]; then
  echo 'MEMORY_V1_V5_SHADOW_TRACE_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260717_memory_v1_v5_shadow_trace_persistence.sql
rollback=ops/sql/20260717_memory_v1_v5_shadow_trace_persistence_rollback.sql
test_sql=tests/memory_v1_v5_shadow_trace_persistence_production_rollback.sql
required_ancestor=40112c14bf63c047168a509027a467afbabc608e
expected_migration_sha=69a0963db1c6182146928bdb033507c2f0f1457014514ea176f855dc18921dc5
expected_test_sha=65b5f951debec8b6c8cce97b7b8d100fab6d64d865b93bf44dd9dc2cb95b09ec
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_shadow_trace_install.lock
phase=initialization
status_file=
schema_installed=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
  echo 'production install requires a clean Git worktree' >&2
  exit 1
}
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

exec 9>"$lock_file"
flock -n 9 || {
  echo 'another V5 shadow-trace install holds the lock' >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_shadow_trace_install_${run_id}.status"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

run_sql_file() {
  docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=120s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
    <"$repo_root/$1"
}

record_exit() {
  exit_code=$?
  if [[ "$exit_code" -ne 0 && "$schema_installed" -eq 1 ]]; then
    phase=automatic_rollback_after_failure
    run_sql_file "$rollback" \
      >"$snapshot_dir/memory_v1_v5_shadow_trace_rollback_${run_id}.log" 2>&1 \
      || true
  fi
  {
    printf 'run_id=%s\n' "$run_id"
    printf 'phase=%s\n' "$phase"
    printf 'exit_code=%s\n' "$exit_code"
    printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$status_file"
  chmod 0600 "$status_file"
}
trap record_exit EXIT

qdrant_signature() {
  curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id)' | sha256sum | awk '{print $1}'
}

capture_existing_memory_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || exit 1
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(t)::text row_json FROM memory.\"$table\" t
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
      AND table_name<>'v5_shadow_trace_event'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regclass('memory.v5_shadow_trace_event') IS NULL
  AND to_regprocedure('memory.record_v5_shadow_trace_v1(text,text,text,text,text,text,text,text,text,text,text,integer,integer,integer,integer,jsonb,integer,integer,integer,text,integer)') IS NULL
  AND to_regrole('memory_v5_trace_writer') IS NULL
)::int")" == 1 ]]

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_shadow_trace_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_shadow_trace_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_v5_shadow_trace_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_v5_shadow_trace_post_${run_id}.tsv"
capture_existing_memory_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=schema_install
install_log="$snapshot_dir/memory_v1_v5_shadow_trace_install_${run_id}.log"
run_sql_file "$migration" >"$install_log" 2>&1
schema_installed=1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=postflight
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_shadow_trace_event")" == 0 ]]
[[ "$(psql_scalar "SELECT (
  (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class
    WHERE oid='memory.v5_shadow_trace_event'::regclass)
  AND NOT has_table_privilege('brains_app','memory.v5_shadow_trace_event','SELECT')
  AND NOT has_table_privilege('brains_app','memory.v5_shadow_trace_event','INSERT')
  AND has_function_privilege(
    'brains_app',
    'memory.record_v5_shadow_trace_v1(text,text,text,text,text,text,text,text,text,text,text,integer,integer,integer,integer,jsonb,integer,integer,integer,text,integer)',
    'EXECUTE'
  )
)::int")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM information_schema.columns
  WHERE table_schema='memory' AND table_name='v5_shadow_trace_event'
    AND column_name IN ('answer','answer_text','canonical_text','claim','claims',
      'evidence','message','prompt','query','query_preview','system_prompt','text')")" == 0 ]]
capture_existing_memory_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo 'existing Memory V1 state changed during trace installation' >&2
  exit 1
}
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo 'Qdrant changed during trace installation' >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_v5_shadow_trace_install_${run_id}.report"
{
  printf 'contract_version=memory_v1_v5_shadow_trace_install_v1\n'
  printf 'head=%s\n' "$(git -C "$repo_root" rev-parse HEAD)"
  printf 'backup=%s\n' "$backup"
  printf 'baseline=%s\n' "$baseline"
  printf 'post=%s\n' "$post"
  printf 'qdrant_before_sha256=%s\n' "$qdrant_before"
  printf 'qdrant_after_sha256=%s\n' "$qdrant_after"
  printf 'initial_trace_rows=0\n'
  printf 'rollback_probe_rows_after=0\n'
  printf 'existing_memory_unchanged=true\n'
  printf 'prompt_influence=false\n'
} >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
schema_installed=0
phase=complete
printf 'memory_v1_v5_shadow_trace_persistence_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
