#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the governed write-boundary ACL migration and
# retires the drained legacy governance timer. Live security writes roll back.

if [[ "${MEMORY_V1_GOVERNED_WRITE_BOUNDARY_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_GOVERNED_WRITE_BOUNDARY_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
retired_timer=memory-v1-governance.timer
governance_owner=557ea042-cb82-48f8-9429-472e96c957ef
migration=ops/sql/20260719_memory_v1_governed_write_boundary.sql
rollback=ops/sql/20260719_memory_v1_governed_write_boundary_rollback.sql
test_sql=tests/memory_v1_governed_write_boundary.sql
clone_test=tools/memory_v1_governed_write_boundary_production_clone.sh
lock_file=/home/ubuntu/brains/.memory_v1_governed_write_boundary_install.lock

declare -A expected_sha256=(
  ["$migration"]="0633c7a55f419a7f7ce34926028c42a08c86fd96bea740f713e37978de851839"
  ["$rollback"]="d513a1f485c11c1743912e5c69c15390222b4a02944d1505de143bd40d532c3e"
  ["$test_sql"]="88af8ecd74777794f5bce894ff9cf67b4fb5bf8a97e087cbd7ac18ee695ad861"
  ["$clone_test"]="5156b5a739aab7afeecba97a37a1b83759df8c202dc718f3e07ea7a5e2e18a83"
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
schema_installed=0
installation_committed=0
retire_governance=0
timer_state=$(mktemp /tmp/memory-v1-write-boundary-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-write-boundary-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-write-boundary-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-write-boundary-after.XXXXXX.tsv)
chmod 0600 "$timer_state" "$table_list" "$before" "$after"

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database" "$@"
}

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    if [[ "$retire_governance" -eq 1 && "$unit" == "$retired_timer" ]]; then
      sudo -n systemctl stop "$unit"
      sudo -n systemctl disable "$unit" >/dev/null
      [[ "$(systemctl is-enabled "$unit")" == disabled ]]
      [[ "$(systemctl is-active "$unit")" == inactive ]]
      continue
    fi
    if [[ "$enabled" == enabled ]]; then
      sudo -n systemctl enable "$unit" >/dev/null
    else
      [[ "$enabled" == disabled ]]
      sudo -n systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      [[ "$active" == inactive ]]
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  exit_code=$?
  if [[ "$schema_installed" -eq 1 && "$installation_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || exit_code=1
  fi
  if [[ "$installation_committed" -eq 0 ]]; then
    retire_governance=0
  fi
  restore_timers || exit_code=1
  rm -f "$timer_state" "$table_list" "$before" "$after"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'schema_installed=%s\n' "$schema_installed"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

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

review_root_signature() {
  (cd "$review_root" && find . -type f -print0 | sort -z \
    | xargs -0r sha256sum) | sha256sum | awk '{print $1}'
}

for required in "${!expected_sha256[@]}"; do
  [[ -f "$required" ]]
  [[ "$(sha256sum "$required" | awk '{print $1}')" \
      == "${expected_sha256[$required]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-enabled "$retired_timer")" == enabled ]]
[[ "$(systemctl is-active "$retired_timer")" == active ]]
[[ "$(scalar "WITH actor AS (
  SELECT set_config('app.user_id','$governance_owner',true)
) SELECT count(*) FROM memory.governance_job,actor
  WHERE status<>'completed'")" == 0 ]]

phase=clone_verification
"$clone_test" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_governed_write_boundary_${run_tag}.status"
report="$snapshot_dir/memory_v1_governed_write_boundary_${run_tag}.json"

phase=quiesce_timers
: >"$timer_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$timer_state")" -eq 13 ]]
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_governed_write_boundary_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_governed_write_boundary_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner >"$backup_partial"
[[ -s "$backup_partial" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -X -A -t -U sage -d "$database" -c \
  "SELECT table_schema || E'\\t' || table_name
   FROM information_schema.tables
   WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
   ORDER BY table_schema,table_name" >"$table_list"
capture_rows "$before"
qdrant_before=$(qdrant_signature)
review_before=$(review_root_signature)
claims_before=$(scalar 'SELECT count(*) FROM memory.claim')

phase=install_acl
run_sql <"$migration" >/dev/null
schema_installed=1

phase=rollback_only_security_test
run_sql <"$test_sql" >/dev/null
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]

phase=postflight
[[ "$(scalar "SELECT bool_and(
    has_table_privilege('brains_app',format('memory.%I',name),'SELECT')
    AND NOT has_table_privilege('brains_app',format('memory.%I',name),'INSERT')
    AND NOT has_table_privilege('brains_app',format('memory.%I',name),'UPDATE')
    AND NOT has_table_privilege('brains_app',format('memory.%I',name),'DELETE')
  ) FROM unnest(ARRAY['entity','entity_alias','candidate','claim',
    'claim_revision','claim_evidence','claim_assessment','claim_relation']) name")" == t ]]
capture_rows "$after"
cmp -s "$before" "$after"
[[ "$(review_root_signature)" == "$review_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=retire_legacy_and_restore
retire_governance=1
restore_timers
installation_committed=1

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_governed_write_boundary_install_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    checks:{clone_verification:true,fresh_backup:true,hash_locked_inputs:true,
      exact_nonlegacy_timer_restoration:true,legacy_governance_timer_retired:true,
      rollback_only_live_security_test:true,zero_memory_row_changes:true,
      owner_isolation:true,controlled_writer_apis_preserved:true,
      governed_direct_dml_revoked:true,claims_unchanged:true,
      qdrant_unchanged:true,review_files_unchanged:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_shadow_allowlist_expansion_or_prompt_influence"}' \
  >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|map(.value==true)|all' "$report" >/dev/null
sha256sum "$report" >"$report.sha256"

phase=complete
printf '%s\n' 'memory_v1_governed_write_boundary_production_install: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
