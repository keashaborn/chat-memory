#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs neutral parent/sibling claim rendering and
# selector support. No assessment is changed and no plan, claim, vector, or
# prompt record is written.

if [[ "${MEMORY_V1_V5_RELATIONSHIP_CLAIM_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_RELATIONSHIP_CLAIM_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
migration=ops/sql/20260719_memory_v1_v5_relationship_claim_projection.sql
rollback=ops/sql/20260719_memory_v1_v5_relationship_claim_projection_rollback.sql
sql_test=tests/memory_v1_v5_relationship_claim_projection.sql
local_projection_test=tests/memory_v1_v5_local_claim_projection.sql
preflight=scripts/memory_v1_v5_claim_projection_preflight.py
preflight_test=scripts/memory_v1_v5_claim_projection_preflight_test.py
worker=scripts/memory_v1_v5_local_claim_projection.py
clone_test=tools/memory_v1_v5_relationship_claim_projection_clone.sh
lock_file=/home/ubuntu/brains/.memory_v1_v5_relationship_claim_projection_install.lock

declare -A expected_sha256=(
  ["$migration"]="175620f2cf49050d2aa34584ab637030cb7e837dde0076ead8b341efbbf5c07c"
  ["$rollback"]="03a261b9421bc91a0d76194df6734bdd72d0766efd52df8b7e1e536195ce4977"
  ["$sql_test"]="5184a7f9955307b4b165ad7f7e60735b34089cbbb587caea29dbde7c837f9102"
  ["$local_projection_test"]="8c5c21499f395735931743500a98aac48b0f2eac29bf6ad6c692935f7dfe02ac"
  ["$preflight"]="11839b7a2e6fcac6e9d813fdcb54bc3c7763c9d59e3465508c5bb543fb7dc05a"
  ["$preflight_test"]="5659c0931ad18873f37655c43723b6bd092b5e6a6f902f6bfae3ba2de02cc084"
  ["$worker"]="a2dbac167b99f021b8e8542006b1f468804a8110c933d404cf63139fa22e5a91"
  ["$clone_test"]="e79b02809011f6268b44932c6d32eef736e38d0851d1a510cb549c6b103da8ba"
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
migration_installed=0
installation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-relationship-claim-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-relationship-claim-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-relationship-claim-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-relationship-claim-after.XXXXXX.tsv)
owner_plan=$(mktemp /tmp/memory-v1-v5-relationship-claim-owner.XXXXXX.json)
other_plan=$(mktemp /tmp/memory-v1-v5-relationship-claim-other.XXXXXX.json)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" \
  "$owner_plan" "$other_plan"

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
  if [[ "$migration_installed" -eq 1 && "$installation_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || exit_code=1
  fi
  restore_timers || exit_code=1
  rm -f "$timer_state" "$table_list" "$before" "$after" \
    "$owner_plan" "$other_plan"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
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
[[ "$(scalar "SELECT position('''relationship.parent_of'',''relationship.sibling_of''' IN pg_get_functiondef('memory.preflight_claim_projection_source_v5_1(uuid)'::regprocedure))=0")" == t ]]
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$preflight_test" >/dev/null
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_local_claim_projection_test.py >/dev/null
python3 -m py_compile "$preflight" "$worker"

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_relationship_claim_projection_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_relationship_claim_projection_${run_tag}.json"

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
  existing_service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$existing_service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$existing_service"
done <"$timer_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_relationship_claim_projection_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_relationship_claim_projection_${run_tag}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -X -A -t -U sage -d "$database" -c \
  "SELECT table_schema || E'\\t' || table_name FROM information_schema.tables
   WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
   ORDER BY table_schema,table_name" >"$table_list"
capture_rows "$before"
qdrant_before=$(qdrant_signature)
review_root_before=$(review_root_signature)

phase=install_contract
run_sql <"$migration" >/dev/null
migration_installed=1
run_sql <"$sql_test" >/dev/null
run_sql -v owner_user_id="$owner" <"$local_projection_test" >/dev/null

set -a
source .env
set +a
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" --owner-user-id "$owner" \
  >"$owner_plan"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" --owner-user-id "$other" \
  >"$other_plan"
jq -e '.apply==false and .plans[0].route=="no_work" and
  .database_writes==0 and .local_model_calls==0 and
  .external_model_calls==0 and .claims==0 and .qdrant==0 and
  .prompt_influence==0' "$owner_plan" >/dev/null
jq -e '.apply==false and .plans[0].route=="no_work" and
  .database_writes==0' "$other_plan" >/dev/null

phase=postflight
capture_rows "$after"
cmp -s "$before" "$after"
[[ "$(review_root_signature)" == "$review_root_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=restore_timers
restore_timers

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg owner_sha256 "$(printf %s "$owner" | sha256sum | awk '{print $1}')" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_relationship_claim_projection_install_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    owner_user_id_sha256:$owner_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    policy:{neutral_kinship_language:true,parent_direction_enforced:true,
      sibling_symmetry_supported:true,manual_review_still_required:true},
    checks:{fresh_backup:true,hash_locked_runtime:true,
      cross_owner_rejected:true,all_database_rows_unchanged:true,
      existing_review_files_unchanged:true,qdrant_unchanged:true,
      assessments_changed_zero:true,claim_plans_written_zero:true,
      claims_written_zero:true,prompt_influence_zero:true,
      local_model_calls_zero:true,external_model_calls_zero:true,
      existing_timers_restored:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_deferred_entailment_reassessment_claim_apply_qdrant_or_prompt_influence"}' \
  >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|map(.value==true)|all' "$report" >/dev/null
sha256sum "$report" >"$report.sha256"

installation_committed=1
phase=complete
printf '%s\n' 'memory_v1_v5_relationship_claim_projection_production_install: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
