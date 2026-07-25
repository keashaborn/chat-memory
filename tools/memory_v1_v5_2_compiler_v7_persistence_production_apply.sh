#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the compiler-v7 persistence allowlist and
# requeues exactly the hash-bound correction job whose first deterministic
# packet failed only because persistence still ended at compiler v6.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_COMPILER_V7_PERSISTENCE_RUN:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_COMPILER_V7_PERSISTENCE_RUN=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "$repo_root/.env"
set +a

container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_compiler_v7_persistence.lock
required_ancestor=358f7e30603857bfa426aa2b7ef6b31a1375d7bd
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
job=db94d835-9598-5805-9101-f320e3c87f0f
content=be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4
failure_operation=0a03d151-d1a9-56cd-b319-e408292ce70e
completion_event=1bf5f805-63fb-4938-a0f2-d7e4480fc36a
retry_operation=b7ab9077-331c-4502-91fb-7b9be6bac0a2
old_function_sha=39715a76bec7dceebbe1b15e25d2fff8300d42331a8bfb6c557070fa4f3e53ae
new_function_sha=4feb28bcf1a132a2c6f4b3628d11b3287db9c009090fd1a9ef68289c5af2f37e
migration=ops/sql/20260725_memory_v1_v5_2_compiler_v7_persistence.sql
rollback=ops/sql/20260725_memory_v1_v5_2_compiler_v7_persistence_rollback.sql
test_sql=tests/memory_v1_v5_2_compiler_v7_persistence.sql
clone_test=tools/memory_v1_v5_2_compiler_v7_persistence_clone.sh

declare -A expected_sha256=(
  ["$migration"]="2a8533cc82f75f637286d8fb280e93015b7c7b75ad3ec3c2ae45ab52a2b85f60"
  ["$rollback"]="319236a6ff74f4888e4db78ac26a85566c4d88e2c5b5f4936f372fdbffa89a1a"
  ["$test_sql"]="29b79341ecf1943bf721ca94790e47835120bdb7c730d9c544dc4a284131ac58"
  ["$clone_test"]="310110779c8d1d4d20e81fd3551d36e602e8119fb30bf44f1bbc5ccd035a973e"
)

timer_state=$(mktemp /tmp/memory-v5-2-compiler-v7-persist-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v5-2-compiler-v7-persist-tables.XXXXXX)
before=$(mktemp /tmp/memory-v5-2-compiler-v7-persist-before.XXXXXX)
after=$(mktemp /tmp/memory-v5-2-compiler-v7-persist-after.XXXXXX)
clone_output=$(mktemp /tmp/memory-v5-2-compiler-v7-persist-clone.XXXXXX)
apply_output=$(mktemp /tmp/memory-v5-2-compiler-v7-persist-apply.XXXXXX)
replay_output=$(mktemp /tmp/memory-v5-2-compiler-v7-persist-replay.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" \
  "$clone_output" "$apply_output" "$replay_output"
timers_quiesced=0
migration_installed=0
retry_committed=0
phase=initialization
status_file=

scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 -c "$1" | tr -d '[:space:]'
}

run_sql() {
  docker exec -i "$container" psql -U sage -d "$database" -X \
    -v ON_ERROR_STOP=1 "$@"
}

function_sha() {
  scalar "SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')"
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    else
      systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  rc=$?
  if [[ "$migration_installed" -eq 1 && "$retry_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || rc=1
  fi
  restore_timers || rc=1
  rm -f "$timer_state" "$table_list" "$before" "$after" \
    "$clone_output" "$apply_output" "$replay_output"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\nretry_committed=%s\ncompleted_at=%s\n' \
      "$phase" "$rc" "$retry_committed" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$rc"
}
trap record_exit EXIT

capture_state() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
        ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value) rows")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$table_list"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for artifact in "${!expected_sha256[@]}"; do
  [[ -f "$artifact" ]]
  [[ "$(sha256sum "$artifact" | awk '{print $1}')" == \
    "${expected_sha256[$artifact]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(function_sha)" == "$old_function_sha" ]]

phase=production_clone
bash "$clone_test" >"$clone_output"
[[ "$(tr -d '\r\n' <"$clone_output")" == \
  memory_v1_v5_2_compiler_v7_persistence_clone:\ PASS ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_compiler_v7_persistence_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_compiler_v7_persistence_${run_tag}.json"

phase=inventory_timers
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
timer_count=$(wc -l <"$timer_state")
(( timer_count >= 16 && timer_count <= 32 ))

phase=quiesce_timers
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

phase=fresh_backup
backup="$snapshot_dir/memory_pre_v5_2_compiler_v7_persistence_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
chmod 0600 "$backup"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')

phase=baseline
docker exec "$container" psql -U sage -d "$database" -X -At -F $'\t' \
  -c "SELECT table_schema,table_name FROM information_schema.tables
      WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
        AND NOT (table_schema='memory' AND table_name IN
          ('evidence_extraction_job','evidence_extraction_event'))
      ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
jobs_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')
events_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')
ledger_before=$(scalar 'SELECT count(*) FROM memory.v5_local_inference_event')
packets_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')
qdrant_before=$(qdrant_signature)
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    AND status='skipped' AND attempts=1
    AND last_error='local_inference_rejected: local_persistence_rejected'")" == 1 ]]

phase=install_and_rollback_tests
run_sql <"$migration" >/dev/null
migration_installed=1
run_sql <"$test_sql" >/dev/null
[[ "$(function_sha)" == "$new_function_sha" ]]

run_retry() {
  local output=$1
  psql "$POSTGRES_DSN" -X -q -A -t -F '|' -v ON_ERROR_STOP=1 \
    >"$output" <<SQL
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT * FROM memory.requeue_owner_local_persistence_failure_v1(
  '$retry_operation','$job','$content','$failure_operation',
  '$completion_event',1,'compiler_persistence_compatibility'
);
COMMIT;
SQL
  chmod 0600 "$output"
}

phase=requeue
run_retry "$apply_output"
retry_committed=1
[[ "$(grep -c '|pending|1|applied$' "$apply_output")" -eq 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    AND status='pending' AND attempts=1 AND last_error IS NULL
    AND lease_token IS NULL AND lease_expires_at IS NULL")" == 1 ]]

phase=replay
run_retry "$replay_output"
[[ "$(grep -c '|pending|1|replayed$' "$replay_output")" -eq 1 ]]

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')" == \
  "$jobs_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" == \
  "$((events_before+1))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_inference_event')" == \
  "$ledger_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')" == \
  "$packets_before" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$owner'::uuid AND operation_id='$retry_operation'::uuid
    AND event_type='queued' AND from_status='skipped' AND to_status='pending'
    AND actor_ref='local_persistence_retry'")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$other'::uuid AND operation_id='$retry_operation'::uuid")" == 0 ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

phase=restore_timers
restore_timers

phase=report
jq -n \
  --arg contract_version memory_v1_v5_2_compiler_v7_persistence_apply_v1 \
  --arg commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_before" \
  --argjson timer_count "$timer_count" \
  '{
    contract_version:$contract_version,outcome:"pass",commit:$commit,
    compatibility:{compiler_v7_persistence:true},
    retry:{jobs_updated:1,events_appended:1,zero_write_replay:true},
    account_isolation_proved:true,non_target_rows_unchanged:true,
    ledger_unchanged:true,packets_unchanged:true,qdrant_unchanged:true,
    model_calls:{local:0,external:0},claims:0,retrieval:0,prompt_influence:0,
    timer_count:$timer_count,timers_restored:true,
    backup:{path:$backup,sha256:$backup_sha256},
    qdrant_sha256:$qdrant_sha256
  }' >"$report"
chmod 0600 "$report"
report_sha=$(sha256sum "$report" | awk '{print $1}')
printf '%s  %s\n' "$report_sha" "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_2_compiler_v7_persistence: PASS\n'
printf 'report=%s\nreport_sha256=%s\nbackup=%s\nbackup_sha256=%s\n' \
  "$report" "$report_sha" "$backup" "$backup_sha"
