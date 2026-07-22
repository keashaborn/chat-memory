#!/usr/bin/env bash
set -euo pipefail

# Server: seebx backend. Installs only additive V5.2 schema/functions and
# rollback-tests their security boundary. It stops before any live V5.2 data,
# local inference, projection, Qdrant, retrieval, or prompt influence.

if [[ $# -ne 2 ]]; then
  echo "usage: $0 INSTALL_PLAN.json AUTHORIZATION.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_V5_2_SCHEMA_INSTALL:-}" != authorized ]]; then
  echo "MEMORY_V1_V5_2_SCHEMA_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
plan=$(realpath "$1")
authorization=$(realpath "$2")
expected_plan="$repo_root/ops/manifests/memory_v1_v5_2_schema_install_plan_20260722.json"
verifier="$repo_root/scripts/memory_v1_v5_2_schema_install_plan.py"
generator="$repo_root/scripts/memory_v1_predicate_registry_v5_2.py"
container=brains-postgres-1
database=memory
database_role=sage
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_schema_install.lock
env_file=/opt/chat-memory/.env
phase=initialization
status_file=
timer_state=
timers_quiesced=0

[[ "$plan" == "$expected_plan" ]] || {
  echo "install plan must be the committed canonical plan" >&2
  exit 1
}
[[ "$authorization" != "$repo_root"/* ]] || {
  echo "authorization file must be outside the Git worktree" >&2
  exit 1
}
[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
  echo "production install requires a clean Git worktree" >&2
  exit 1
}
python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" \
  --authorization "$authorization" >/dev/null

exec 9>"$lock_file"
flock -n 9 || {
  echo "another V5.2 schema install holds $lock_file" >&2
  exit 1
}
umask 077

auth_id=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' \
  "$authorization")
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_v5_2_schema_install_${run_id}.status"
timer_state="$snapshot_dir/memory_v1_v5_2_schema_install_timers_${run_id}.tsv"

restore_timers() {
  [[ "$timers_quiesced" -eq 1 && -s "$timer_state" ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]] || return 1
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]] || return 1
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      [[ "$active" == inactive ]] || return 1
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  exit_code=$?
  if [[ "$timers_quiesced" -eq 1 ]]; then
    failed_phase=$phase
    phase=restore_timers_after_failure
    restore_timers || exit_code=1
    phase=$failed_phase
  fi
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_id=%s\n' "$run_id"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U "$database_role" -d "$database" -c "$1"
}

run_sql_file() {
  local file=$1
  docker exec \
    -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=240s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U "$database_role" -d "$database" <"$repo_root/$file"
}

run_sql_path() {
  local file=$1
  docker exec \
    -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=240s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U "$database_role" -d "$database" <"$file"
}

authenticated_health() {
  set -a
  source "$env_file"
  set +a
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  [[ "$(systemctl is-active brains.service)" == active ]]
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_memory_state() {
  local table_list=$1 output=$2 state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || return 1
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

phase=source_preflight
authenticated_health
preflight="$snapshot_dir/memory_v1_v5_2_schema_preflight_${run_id}.json"
python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" \
  --authorization "$authorization" --production-preflight --output "$preflight"

mapfile -t migrations < <(
  python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" \
    --list-kind migration
)
mapfile -t tests < <(
  python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" \
    --list-kind rolled_back_test
)
[[ ${#migrations[@]} -eq 5 ]]
[[ ${#tests[@]} -eq 5 ]]

phase=capture_timer_state
mapfile -t timers < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ ${#timers[@]} -gt 0 ]]
: >"$timer_state"
for unit in "${timers[@]}"; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done
chmod 0600 "$timer_state"

phase=quiesce_memory_v1_timers
for unit in "${timers[@]}"; do sudo -n systemctl stop "$unit"; done
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done <"$timer_state"

phase=capture_baseline
table_list="$snapshot_dir/memory_v1_v5_2_schema_protected_tables_${run_id}.txt"
before_state="$snapshot_dir/memory_v1_v5_2_schema_before_${run_id}.tsv"
after_state="$snapshot_dir/memory_v1_v5_2_schema_after_${run_id}.tsv"
psql_scalar "
  SELECT table_name
  FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name NOT IN (
      'predicate_registry_version','predicate_registry_seed','predicate_contract'
    )
  ORDER BY table_name
" >"$table_list"
chmod 0600 "$table_list"
capture_memory_state "$table_list" "$before_state"
qdrant_before=$(qdrant_signature)

phase=fresh_backup
database_size=$(psql_scalar "SELECT pg_database_size(current_database())")
free_bytes=$(df --output=avail -B1 "$snapshot_dir" | tail -1 | tr -d '[:space:]')
(( free_bytes >= database_size * 2 )) || {
  echo "insufficient free space for required backup margin" >&2
  exit 1
}
backup_partial="$snapshot_dir/.memory_pre_v5_2_schema_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_schema_${run_id}.dump"
catalog="$backup.catalog"
checksum="$backup.sha256"
docker exec "$container" pg_dump -U "$database_role" -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$checksum"
chmod 0600 "$checksum"

phase=revalidate_before_schema_write
python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" \
  --authorization "$authorization" >/dev/null

registry_sql_one="$snapshot_dir/memory_v1_v5_2_registry_${run_id}.sql"
registry_sql_two="$snapshot_dir/memory_v1_v5_2_registry_replay_${run_id}.sql"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$generator" \
  --emit-install-sql >"$registry_sql_one"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$generator" \
  --emit-install-sql >"$registry_sql_two"
cmp -s "$registry_sql_one" "$registry_sql_two"
chmod 0600 "$registry_sql_one" "$registry_sql_two"

phase=install_schema
install_log="$snapshot_dir/memory_v1_v5_2_schema_install_${run_id}.log"
: >"$install_log"
run_sql_path "$registry_sql_one" >>"$install_log" 2>&1
run_sql_path "$registry_sql_two" >>"$install_log" 2>&1
for migration in "${migrations[@]}"; do
  printf 'INSTALL %s\n' "$migration" >>"$install_log"
  run_sql_file "$migration" >>"$install_log" 2>&1
  run_sql_file "$migration" >>"$install_log" 2>&1
done

phase=rolled_back_security_tests
for test_file in "${tests[@]}"; do
  printf 'TEST %s\n' "$test_file" >>"$install_log"
  run_sql_file "$test_file" >>"$install_log" 2>&1
done
chmod 0600 "$install_log"

phase=postflight
capture_memory_state "$table_list" "$after_state"
cmp -s "$before_state" "$after_state" || {
  diff -u "$before_state" "$after_state" >&2 || true
  echo "protected Memory V1 records changed" >&2
  exit 1
}
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(psql_scalar "
  SELECT (
    (SELECT count(*) FROM memory.predicate_registry_version
      WHERE registry_version='memory_predicate_registry_v5_2')=1
    AND (SELECT count(*) FROM memory.predicate_contract
      WHERE registry_version='memory_predicate_registry_v5_2')=85
    AND (SELECT count(*) FROM memory.relationship_predicate_contract_v5_2)=41
    AND (SELECT count(*) FROM memory.predicate_registry_source_binding_v5_2)=1
    AND NOT EXISTS (SELECT 1 FROM memory.observation
      WHERE predicate_registry_version='memory_predicate_registry_v5_2')
    AND NOT EXISTS (SELECT 1 FROM memory.entity_resolution_plan
      WHERE predicate_registry_version='memory_predicate_registry_v5_2')
    AND NOT EXISTS (SELECT 1 FROM memory.projection_plan
      WHERE predicate_registry_version='memory_predicate_registry_v5_2')
    AND NOT EXISTS (SELECT 1 FROM memory.entity_resolution_reconciliation_v5_2)
    AND NOT EXISTS (SELECT 1 FROM memory.claim_relation_v5)
    AND NOT EXISTS (SELECT 1 FROM memory.preference_relation_v5)
    AND NOT EXISTS (SELECT 1 FROM memory.project_knowledge_relation_v5)
    AND NOT EXISTS (SELECT 1 FROM memory.projection_dispatch_v5)
    AND NOT EXISTS (SELECT 1 FROM memory.predicate_registry_version
      WHERE registry_version='memory_predicate_registry_v5_2' AND runtime_active)
  )::integer
")" == 1 ]]

phase=restore_timers
restore_timers
authenticated_health

phase=complete
report="$snapshot_dir/memory_v1_v5_2_schema_install_${run_id}.json"
jq -n \
  --arg contract_version memory_v1_v5_2_schema_install_report_v1 \
  --arg run_id "$run_id" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg plan_sha256 "$(sha256sum "$plan" | awk '{print $1}')" \
  --arg backup "$backup" \
  --arg qdrant_sha256 "$qdrant_before" \
  '{contract_version:$contract_version,run_id:$run_id,
    head_commit:$head_commit,plan_sha256:$plan_sha256,backup:$backup,
    schema_installed:true,governed_v5_2_rows_created:0,
    qdrant_before_sha256:$qdrant_sha256,qdrant_unchanged:true,
    timers_restored:true,runtime_activated:false,prompt_influence:false,
    hard_stop:"before_live_v5_2_extraction_staging_projection_or_retrieval"}' \
  >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
printf '%s\n' 'memory_v1_v5_2_schema_production_install: PASS'
