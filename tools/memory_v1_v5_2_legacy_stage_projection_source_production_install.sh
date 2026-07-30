#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the reviewed V5-to-V5.2 projection-source
# compatibility functions and produces an exact, read-only claim-target review.
# It creates no governed rows, vectors, retrieval changes, or prompt influence.

if [[ "${MEMORY_V1_V5_2_LEGACY_STAGE_SOURCE_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_LEGACY_STAGE_SOURCE_INSTALL=authorized is required' >&2
  exit 1
fi
if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
first_admission=8c751d72-073c-4b33-9431-72838f374003
second_admission=fd48e387-406e-47a8-b87a-b15320d63487
policy_held=1a498c5b-29ee-4b91-a038-7cc3987162f9
expected_target_sha256=a8400e1d233c9ecde4a22420e6705d31166a14e4013cffbc487e13bbaa951885
required_ancestor=673dcd6
migration=ops/sql/20260730_memory_v1_v5_2_legacy_stage_projection_source_compat_v1.sql
rollback=ops/sql/20260730_memory_v1_v5_2_legacy_stage_projection_source_compat_v1_rollback.sql
clone_test=tools/memory_v1_v5_legacy_stage_claim_target_review_clone.sh
reviewer=scripts/memory_v1_v5_2_claim_target_review.py
entailment_service_source=ops/systemd/memory-v1-v5-local-entailment.service
entailment_service_live=/etc/systemd/system/memory-v1-v5-local-entailment.service
python_bin=/opt/chat-memory/venv/bin/python
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_legacy_stage_source_install.lock
migration_sha256=f065af7daa69b2d5e396e73def8f1c6aa66807d447ac49dd37d91651318433b3
rollback_sha256=c0f8b984e7f434c04bb04ebd8ce49dee72219d1b55c357da0ed9532d0b941cf6
clone_test_sha256=1c95f51c53cbbded825f28e4da12d17026aba246905a4b9473ee49a34fa7ad1a
entailment_service_sha256=01cc9b84e22e2ed3f8b817895dc3e6bd34589453fda6a90711cdeaa5fa3ee725
entailment_service_prior_sha256=9dd02f66f0240e491a04b45471f998fbbd551666371cd5bf494c8260d35d3340

timer_state=$(mktemp /tmp/memory-v1-v5-2-legacy-source-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-legacy-source-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-2-legacy-source-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-2-legacy-source-after.XXXXXX)
clone_output=$(mktemp /tmp/memory-v1-v5-2-legacy-source-clone.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" "$clone_output"
timers_quiesced=0
migration_installed=0
installation_committed=0
entailment_service_installed=0
phase=initialization
status_file=
entailment_service_backup=

set -a
source "$repo_root/.env"
set +a

run_sql() {
  docker exec -i "$container" psql -U sage -d "$database" -X \
    -v ON_ERROR_STOP=1 "$@"
}

scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 -c "$1" | tr -d '[:space:]'
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    else
      [[ "$enabled" == disabled ]]
      systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      [[ "$active" == inactive ]]
      systemctl stop "$unit"
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
  if [[ "$entailment_service_installed" -eq 1 \
        && "$installation_committed" -eq 0 \
        && -n "$entailment_service_backup" ]]; then
    install -o root -g root -m 0644 \
      "$entailment_service_backup" "$entailment_service_live" \
      || exit_code=1
    systemctl daemon-reload || exit_code=1
    systemctl reset-failed memory-v1-v5-local-entailment.service \
      >/dev/null 2>&1 || true
  fi
  restore_timers || exit_code=1
  rm -f "$timer_state" "$table_list" "$before" "$after" "$clone_output"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$phase" "$exit_code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
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
    | sha256sum | cut -d' ' -f1
}

[[ "$(sha256sum "$migration" | cut -d' ' -f1)" == "$migration_sha256" ]]
[[ "$(sha256sum "$rollback" | cut -d' ' -f1)" == "$rollback_sha256" ]]
[[ "$(sha256sum "$clone_test" | cut -d' ' -f1)" == "$clone_test_sha256" ]]
[[ "$(sha256sum "$entailment_service_source" | cut -d' ' -f1)" \
  == "$entailment_service_sha256" ]]
[[ "$(sha256sum "$entailment_service_live" | cut -d' ' -f1)" \
  == "$entailment_service_prior_sha256" ]]
[[ -x "$clone_test" && -f "$reviewer" ]]
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(systemctl is-active brains.service)" == active ]]
mapfile -t failed_memory_units < <(
  systemctl list-units --failed --no-legend --no-pager \
    'memory-v1-*.service' \
    | awk '{print $1}'
)
[[ "${#failed_memory_units[@]}" == 1 ]]
[[ "${failed_memory_units[0]}" == memory-v1-v5-local-entailment.service ]]
[[ "$(systemctl show memory-v1-v5-local-entailment.service -p Result --value)" \
  == start-limit-hit ]]
[[ "$(systemctl show memory-v1-v5-local-entailment.service \
  -p ExecMainStatus --value)" == 0 ]]

phase=production_clone_proof
"$clone_test" >"$clone_output"
grep -Fxq 'LEGACY_STAGE_CLAIM_TARGET_REVIEW_CLONE=COMPLETE' "$clone_output"
grep -Fxq 'SUCCESS_COUNT=19' "$clone_output"
grep -Fxq 'FAILURE_COUNT=1' "$clone_output"
grep -Fxq 'DATABASE_WRITES=0' "$clone_output"
grep -Fxq 'PRODUCTION_WRITES=0' "$clone_output"

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_legacy_source_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_legacy_source_${run_tag}.json"
artifact_dir="$review_root/legacy-stage-claim-target-production-$run_tag"
install -d -m 0700 "$artifact_dir"

phase=inventory_timers
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
timer_count=$(wc -l <"$timer_state")
(( timer_count >= 15 && timer_count <= 40 ))

phase=quiesce_timers
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 60); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

phase=fresh_backup
partial="$snapshot_dir/.memory_pre_v5_2_legacy_source_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_legacy_source_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
backup_sha256=$(sha256sum "$backup" | cut -d' ' -f1)
printf '%s  %s\n' "$backup_sha256" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"
entailment_service_backup="$snapshot_dir/memory-v1-v5-local-entailment_${run_tag}.service"
install -o root -g root -m 0600 \
  "$entailment_service_live" "$entailment_service_backup"
entailment_service_backup_sha256=$(
  sha256sum "$entailment_service_backup" | cut -d' ' -f1
)
[[ "$entailment_service_backup_sha256" == \
  "$entailment_service_prior_sha256" ]]

phase=baseline
docker exec "$container" psql -U sage -d "$database" -X -At -F $'\t' \
  -c "SELECT table_schema,table_name FROM information_schema.tables
      WHERE table_type='BASE TABLE'
        AND table_schema IN ('memory','public')
      ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)

mapfile -t targets < <(
  docker exec "$container" psql -X -A -t -F '|' -U sage -d "$database" \
    -v ON_ERROR_STOP=1 -c "
      SELECT member.observation_id,member.observation_sha256
      FROM memory.v5_local_legacy_stage_admission_observation AS member
      JOIN memory.v5_local_packet_stage_admission AS stage
        ON stage.owner_user_id=member.owner_user_id
       AND stage.admission_id=member.admission_id
      JOIN memory.observation_entailment_v5 AS entailment
        ON entailment.owner_user_id=member.owner_user_id
       AND entailment.observation_id=member.observation_id
      WHERE member.owner_user_id='$owner'::uuid
        AND member.admission_id IN (
          '$first_admission'::uuid,
          '$second_admission'::uuid
        )
        AND stage.decision='legacy_applied_stage_entailment'
        AND entailment.decision='accepted'
      ORDER BY member.observation_id;"
)
[[ "${#targets[@]}" == 20 ]]
target_sha256=$(printf '%s\n' "${targets[@]}" | sha256sum | cut -d' ' -f1)
[[ "$target_sha256" == "$expected_target_sha256" ]]

phase=install_functions
install -o root -g root -m 0644 \
  "$entailment_service_source" "$entailment_service_live"
entailment_service_installed=1
systemctl daemon-reload
systemctl reset-failed memory-v1-v5-local-entailment.service
[[ "$(systemctl show memory-v1-v5-local-entailment.service \
  -p StartLimitIntervalUSec --value)" == 30min ]]
[[ "$(systemctl show memory-v1-v5-local-entailment.service \
  -p StartLimitBurst --value)" == 8 ]]
run_sql <"$migration" >/dev/null
migration_installed=1
[[ "$(scalar "SELECT to_regprocedure(
  'memory.v5_2_legacy_stage_projection_source_allowed_v1(uuid,uuid,text)'
) IS NOT NULL")" == t ]]
[[ "$(scalar "SELECT pg_get_userbyid(proowner)
  FROM pg_proc WHERE oid='memory.v5_2_legacy_stage_projection_source_allowed_v1(uuid,uuid,text)'::regprocedure")" \
  == memory_v5_legacy_stage_compat_maintainer ]]
[[ "$(scalar "SELECT has_function_privilege(
  'brains_app',
  'memory.v5_2_legacy_stage_projection_source_allowed_v1(uuid,uuid,text)',
  'EXECUTE')")" == f ]]

phase=read_only_claim_target_review
runtime=(
  env
  POSTGRES_DSN="$POSTGRES_DSN"
  PYTHONPATH="$repo_root:$repo_root/scripts"
)
success_files=()
failed_rows=()
for target in "${targets[@]}"; do
  observation=${target%%|*}
  observation_sha256=${target#*|}
  item_report="$artifact_dir/review-$observation.json"
  item_log="$artifact_dir/review-$observation.log"
  if "${runtime[@]}" "$python_bin" "$reviewer" \
    --owner "$owner" --observation "$observation" \
    --output "$item_report" >"$item_log" 2>&1; then
    [[ "$(jq -er '.item_count' "$item_report")" == 1 ]]
    [[ "$(jq -er '.proofs.database_writes' "$item_report")" == 0 ]]
    [[ "$(jq -er '.proofs.local_model_calls' "$item_report")" == 0 ]]
    [[ "$(jq -er '.proofs.external_model_calls' "$item_report")" == 0 ]]
    [[ "$(jq -er '.proofs.claim_writes' "$item_report")" == 0 ]]
    [[ "$(jq -er '.proofs.qdrant_writes' "$item_report")" == 0 ]]
    success_files+=("$item_report")
  else
    [[ ! -e "$item_report" ]]
    failed_rows+=("$observation|$observation_sha256")
  fi
done
[[ "${#success_files[@]}" == 19 ]]
[[ "${#failed_rows[@]}" == 1 ]]
[[ "${failed_rows[0]%%|*}" == "$policy_held" ]]
grep -q 'complete owner-scoped V5.2 projection source not found' \
  "$artifact_dir/review-$policy_held.log"
action_counts=$(
  jq -cs '
    map(.items[0].action)
    | group_by(.)
    | map({key:.[0],value:length})
    | from_entries
  ' "${success_files[@]}"
)
[[ "$action_counts" == '{"create":19}' ]]
predicate_counts=$(
  jq -cs '
    map(.items[0].predicate)
    | group_by(.)
    | map({key:.[0],value:length})
    | from_entries
  ' "${success_files[@]}"
)
[[ "$predicate_counts" == \
  '{"age.reported":1,"health.user_reported_observation":2,"identity.name":5,"pet.breed":1,"pet.coat_color":2,"pet.eye_color":2,"pet.hearing_status":1,"pet.sex":1,"pet.weight_reported":1,"relationship.sibling_of":3}' ]]

cross_owner="$artifact_dir/cross-owner.json"
if "${runtime[@]}" "$python_bin" "$reviewer" \
  --owner "$other_owner" --observation "${targets[0]%%|*}" \
  --output "$cross_owner" >"$artifact_dir/cross-owner.log" 2>&1; then
  echo 'cross-owner production review unexpectedly succeeded' >&2
  exit 1
fi
[[ ! -e "$cross_owner" ]]

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

phase=restore_timers
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-failed memory-v1-v5-local-entailment.service)" \
  != failed ]]
[[ -z "$(
  systemctl list-units --failed --no-legend --no-pager \
    'memory-v1-*.service' \
    | awk '{print $1}'
)" ]]
health=$(
  curl -fsS -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -r '.status'
)
[[ "$health" == ok ]]

installation_committed=1
migration_installed=0
entailment_service_installed=0
phase=report
jq -nS \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg migration_sha256 "$migration_sha256" \
  --arg rollback_sha256 "$rollback_sha256" \
  --arg target_set_sha256 "$target_sha256" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha256" \
  --arg qdrant_sha256 "$qdrant_after" \
  --arg review_artifact_dir "$artifact_dir" \
  --arg entailment_service_sha256 "$entailment_service_sha256" \
  --arg entailment_service_backup "$entailment_service_backup" \
  --arg entailment_service_backup_sha256 \
    "$entailment_service_backup_sha256" \
  --argjson timer_count "$timer_count" \
  --argjson action_counts "$action_counts" \
  --argjson predicate_counts "$predicate_counts" \
  '{
    contract_version:
      "memory_v1_v5_2_legacy_stage_projection_source_install_report_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    migration_sha256:$migration_sha256,
    rollback_sha256:$rollback_sha256,
    target_set_sha256:$target_set_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    entailment_service:{
      installed_sha256:$entailment_service_sha256,
      backup_path:$entailment_service_backup,
      backup_sha256:$entailment_service_backup_sha256,
      start_limit_interval:"30min",
      start_limit_burst:8
    },
    review_artifact_dir:$review_artifact_dir,
    result:{
      targets:20,
      valid_create_targets:19,
      surface_policy_contract_holds:1,
      action_counts:$action_counts,
      predicate_counts:$predicate_counts
    },
    proofs:{
      disposable_clone_passed:true,
      fresh_backup:true,
      exact_timer_states_restored:true,
      all_table_rows_unchanged:true,
      qdrant_unchanged:true,
      cross_owner_rejected:true,
      database_writes:0,
      claim_writes:0,
      qdrant_writes:0,
      local_model_calls:0,
      external_model_calls:0,
      retrieval_changes:0,
      prompt_influence:0,
      rollback_retained:true,
      service_health:true
    },
    timer_count:$timer_count,
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before durable claim materialization"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf '%s\n' \
  'MEMORY_V1_V5_2_LEGACY_STAGE_SOURCE_INSTALL=COMPLETE' \
  "HEAD=$(git rev-parse HEAD)" \
  "MIGRATION_SHA256=$migration_sha256" \
  "ROLLBACK_SHA256=$rollback_sha256" \
  "ENTAILMENT_SERVICE_SHA256=$entailment_service_sha256" \
  "TARGET_SET_SHA256=$target_sha256" \
  "VALID_CREATE_TARGETS=19" \
  "SURFACE_POLICY_CONTRACT_HOLDS=1" \
  'DATABASE_WRITES=0' \
  'CLAIM_WRITES=0' \
  'QDRANT_WRITES=0' \
  'MODEL_CALLS=0' \
  'CROSS_OWNER_REJECTED=1' \
  "REPORT=$report" \
  "BACKUP=$backup"
