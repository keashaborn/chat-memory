#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the owner-only private new-entity validation
# layer. It performs no model call and no data write; the timer is enabled but
# left inactive until the separately audited live canary begins.

if [[ "${MEMORY_V1_V5_LOCAL_ENTITY_VALIDATION_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LOCAL_ENTITY_VALIDATION_INSTALL=authorized is required' >&2
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
service=memory-v1-v5-local-entity-validation.service
timer=memory-v1-v5-local-entity-validation.timer
service_source=ops/systemd/$service
timer_source=ops/systemd/$timer
entity_migration=ops/sql/20260719_memory_v1_v5_local_entity_validation.sql
entity_rollback=ops/sql/20260719_memory_v1_v5_local_entity_validation_rollback.sql
entailment_migration=ops/sql/20260719_memory_v1_v5_local_entailment_validated_stage.sql
entailment_rollback=ops/sql/20260719_memory_v1_v5_local_entailment_validated_stage_rollback.sql
production_test=tests/memory_v1_v5_local_entity_validation.sql
provider=scripts/memory_v1_v5_local_entity_validation_provider.py
worker=scripts/memory_v1_v5_local_entity_validation.py
provider_test=scripts/memory_v1_v5_local_entity_validation_test.py
scheduler_test=scripts/memory_v1_v5_local_entity_validation_scheduler_test.py
smoke=scripts/memory_v1_v5_local_entity_validation_smoke.py
entailment_worker=scripts/memory_v1_v5_local_entailment_scheduler.py
clone_test=tools/memory_v1_v5_local_entity_validation_clone.sh
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_entity_validation_install.lock

declare -A expected_sha256=(
  ["$entity_migration"]="27f62e3396554451fcfc2f545d91862eeeff639cc4801ed92e39dfefe864f67a"
  ["$entity_rollback"]="789072837893ab069c8c5b27da4f2936d2582da803330bfacf3cb86823171b2b"
  ["$entailment_migration"]="8ddd7b5155803530912ee13b8faf2ff7cad00ca1350d75f4679643db8cb70aad"
  ["$entailment_rollback"]="9d8e9f45fa8cc13f3d5efb08da51eb5e8073d52aa287def355b2837b637c277a"
  ["$production_test"]="9cfc1c02007afdaea57daf74ece71413d0369a7b5af638ef55ebdc37fce57477"
  ["$provider"]="f68947418fe42db1197764cacb12eb0cbf9ce6429529e1475f54602e739383b7"
  ["$worker"]="dd2f5cf16150b052f5d6f1cc94bfc170777fcb510b953dc9ffb030f7c85c30ff"
  ["$provider_test"]="d96475e60a4eba00f7aaaae2c408c69c5b5198015f4394ba80be0a613c42406c"
  ["$scheduler_test"]="c280385073e9fbc4d750f9ab3fee77a36c43a597b0eb59f941fabbe642d17879"
  ["$smoke"]="a1bf289da229901afd7f132f3f17c272407a8045f99e930d68af48d8306af898"
  ["$entailment_worker"]="0a6005e07edacc0ffd7e72017c1555164d594b12c2089aa84892ca5412bc9023"
  ["$service_source"]="4027a0b0ac5191c3e90cf395447ef0cbb9a96ffaddaec58586fb56f0a7529fd0"
  ["$timer_source"]="35ac30f0dc05b39f03a75f863e14032e5dd59f0252882d78a97cfb6a43226a25"
  ["$clone_test"]="1065f834384694d820f71f84b2d9ce536281543e658cf265a5012b2676418857"
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
migrations_installed=0
units_installed=0
installation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-local-entity-validation-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-local-entity-validation-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-local-entity-validation-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-local-entity-validation-after.XXXXXX.tsv)
owner_plan=$(mktemp /tmp/memory-v1-v5-local-entity-validation-owner.XXXXXX.json)
other_plan=$(mktemp /tmp/memory-v1-v5-local-entity-validation-other.XXXXXX.json)
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

query_rows() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
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
  if [[ "$units_installed" -eq 1 && "$installation_committed" -eq 0 ]]; then
    sudo -n systemctl disable --now "$timer" >/dev/null 2>&1 || true
    sudo -n rm -f "/etc/systemd/system/$service" "/etc/systemd/system/$timer"
    sudo -n systemctl daemon-reload
  fi
  if [[ "$migrations_installed" -eq 1 && "$installation_committed" -eq 0 ]]; then
    run_sql <"$entailment_rollback" >/dev/null 2>&1 || exit_code=1
    run_sql <"$entity_rollback" >/dev/null 2>&1 || exit_code=1
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
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
[[ ! -e "/etc/systemd/system/$service" ]]
[[ ! -e "/etc/systemd/system/$timer" ]]
[[ "$(scalar "SELECT to_regclass('memory.v5_local_entity_validation_assessment') IS NULL")" == t ]]
[[ "$(scalar "SELECT to_regclass('memory.v5_local_validated_stage_admission') IS NULL")" == t ]]
[[ "$(stat -c '%a:%U:%G' "$review_root")" == 700:ubuntu:ubuntu ]]
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$provider_test" >/dev/null
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$scheduler_test" >/dev/null
python3 -m py_compile "$provider" "$worker" "$smoke"
systemd-analyze verify "$service_source" "$timer_source"

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_local_entity_validation_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_local_entity_validation_${run_tag}.json"

phase=quiesce_timers
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" != "$timer" ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$timer_state")" -eq 12 ]]
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
backup_partial="$snapshot_dir/.memory_pre_v5_local_entity_validation_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_local_entity_validation_${run_tag}.dump"
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
query_rows "SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)
review_root_before=$(review_root_signature)

phase=install_schema
run_sql <"$entity_migration" >/dev/null
migrations_installed=1
run_sql <"$entailment_migration" >/dev/null
run_sql -v owner_user_id="$owner" <"$production_test" >/dev/null
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_entity_validation_assessment')" == 0 ]]
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_validated_stage_admission')" == 0 ]]

set -a
source .env
set +a
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" >"$owner_plan"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$other" >"$other_plan"
jq -e '.apply==false and .plans[0].route=="validate_new_entity" and
  .database_writes==0 and .local_model_calls==0 and
  .external_model_calls==0 and .qdrant_writes==0 and
  .claim_writes==0 and .prompt_influence==0' "$owner_plan" >/dev/null
jq -e '.apply==false and .plans[0].route=="no_work" and
  .database_writes==0 and .local_model_calls==0' "$other_plan" >/dev/null

phase=install_disabled_units
sudo -n install -o root -g root -m 0644 "$service_source" \
  "/etc/systemd/system/$service"
sudo -n install -o root -g root -m 0644 "$timer_source" \
  "/etc/systemd/system/$timer"
sudo -n systemctl daemon-reload
units_installed=1
[[ "$(systemctl is-enabled "$timer")" == disabled ]]
[[ "$(systemctl is-active "$timer")" == inactive ]]

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_entity_validation_assessment')" == 0 ]]
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_validated_stage_admission')" == 0 ]]
[[ "$(review_root_signature)" == "$review_root_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=restore_timers
restore_timers

phase=enable_timer_without_start
sudo -n systemctl enable "$timer" >/dev/null
[[ "$(systemctl is-enabled "$timer")" == enabled ]]
[[ "$(systemctl is-active "$timer")" == inactive ]]
[[ "$(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u | wc -l)" -eq 13 ]]

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg owner_sha256 "$(printf %s "$owner" | sha256sum | awk '{print $1}')" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_local_entity_validation_install_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    owner_user_id_sha256:$owner_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    canary:{outcome:"dry_route_verified",local_model_calls:0,
      external_model_calls:0,production_data_writes:0},
    policy:{version:"memory_v1_v5_local_entity_validation_policy_v1",
      exact_source_span_required:true,private_validation_required:true,
      deterministic_stage_after_acceptance:true,max_packets_per_cycle:1},
    checks:{fresh_backup:true,hash_locked_runtime:true,forced_rls:true,
      append_only:true,function_only_write_access:true,
      cross_owner_rejected:true,all_preexisting_rows_unchanged:true,
      assessment_rows_zero:true,validated_stage_rows_zero:true,
      existing_review_files_unchanged:true,qdrant_unchanged:true,
      entity_writes_zero:true,claims_written_zero:true,
      prompt_influence_zero:true,local_model_calls_zero:true,
      external_model_calls_zero:true,existing_timers_restored:true,
      entity_validation_timer_enabled_not_started:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_private_entity_validation_claim_projection_qdrant_or_prompt_influence"}' \
  >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|map(.value==true)|all' "$report" >/dev/null
sha256sum "$report" >"$report.sha256"

installation_committed=1
phase=complete
printf '%s\n' 'memory_v1_v5_local_entity_validation_production_install: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
