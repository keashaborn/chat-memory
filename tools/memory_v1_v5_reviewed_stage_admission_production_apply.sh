#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the additive reviewed-stage admission contract
# and appends exactly two owner-scoped admission/audit rows. No model, claim,
# Qdrant, retrieval, projection, or prompt writes occur.

if [[ "${MEMORY_V1_V5_REVIEWED_STAGE_PRODUCTION_RUN:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_REVIEWED_STAGE_PRODUCTION_RUN=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_reviewed_stage.lock
required_ancestor=419a713b6a2bd93d322466a237b9138b7627bd48
owner=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
migration=ops/sql/20260719_memory_v1_v5_reviewed_stage_admission.sql
rollback=ops/sql/20260719_memory_v1_v5_reviewed_stage_admission_rollback.sql
worker=scripts/memory_v1_v5_reviewed_stage_admission.py
entailment=scripts/memory_v1_v5_local_entailment_scheduler.py
clone_test=tools/memory_v1_v5_reviewed_stage_admission_clone.sh

declare -A expected_sha256=(
  ["$migration"]="933b3f282825422b71d55741abdc252c08123aef5c1997437319db63901cb50f"
  ["$rollback"]="b78594e2e28c94723e1d337ea5810e26172148cca9bc6f5570a753145501b4a1"
  ["$worker"]="84628662f0fc88fbbeec65c9f4044c4455d9a38f0912ab4801e2551f740f7972"
  ["$clone_test"]="94e11a43b873daaf51686866b6b30372412a3686970be49f2d3dc042c389e8a5"
)

timer_state=$(mktemp /tmp/memory-v5-reviewed-stage-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v5-reviewed-stage-tables.XXXXXX)
before=$(mktemp /tmp/memory-v5-reviewed-stage-before.XXXXXX)
after=$(mktemp /tmp/memory-v5-reviewed-stage-after.XXXXXX)
dry=$(mktemp /tmp/memory-v5-reviewed-stage-dry.XXXXXX)
applied=$(mktemp /tmp/memory-v5-reviewed-stage-applied.XXXXXX)
entailment_dry=$(mktemp /tmp/memory-v5-reviewed-stage-entailment.XXXXXX)
clone_output=$(mktemp /tmp/memory-v5-reviewed-stage-clone.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" "$dry" \
  "$applied" "$entailment_dry" "$clone_output"
timers_quiesced=0
migration_installed=0
writes_committed=0
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

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$enabled" == enabled ]] && sudo -n systemctl enable "$unit" >/dev/null \
      || sudo -n systemctl disable "$unit" >/dev/null
    [[ "$active" == active ]] && sudo -n systemctl start "$unit" \
      || sudo -n systemctl stop "$unit"
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  rc=$?
  if [[ "$migration_installed" -eq 1 && "$writes_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || rc=1
  fi
  restore_timers || rc=1
  rm -f "$timer_state" "$table_list" "$before" "$after" "$dry" \
    "$applied" "$entailment_dry" "$clone_output"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\nwrites_committed=%s\ncompleted_at=%s\n' \
      "$phase" "$rc" "$writes_committed" \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$status_file"
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

stage_existing_signature() {
  scalar "SELECT count(*)::text || E'\\t' ||
    encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
      ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM (SELECT to_jsonb(value)::text AS row_json
      FROM memory.v5_local_packet_stage_admission AS value
      WHERE NOT (owner_user_id='$owner'::uuid
        AND decision='reviewed_entity_stage')) rows"
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
[[ -f "$entailment" ]]
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
python3 -m py_compile "$worker"
[[ "$(systemctl --failed --no-legend --no-pager | wc -l)" -eq 0 ]]

phase=production_clone
bash "$clone_test" >"$clone_output"
[[ "$(tail -n 1 "$clone_output" | tr -d '\r\n')" == \
  'memory_v1_v5_reviewed_stage_admission_clone: PASS' ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_reviewed_stage_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_reviewed_stage_${run_tag}.json"

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

phase=fresh_backup
partial="$snapshot_dir/.memory_pre_v5_reviewed_stage_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_reviewed_stage_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -U sage -d "$database" -X -At -F $'\t' \
  -c "SELECT table_schema,table_name FROM information_schema.tables
      WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
        AND NOT (table_schema='memory' AND table_name IN
          ('v5_local_packet_stage_admission',
           'v5_local_reviewed_stage_admission'))
      ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
stage_count_before=$(scalar \
  'SELECT count(*) FROM memory.v5_local_packet_stage_admission')
stage_existing_before=$(stage_existing_signature)
qdrant_before=$(qdrant_signature)

phase=install
run_sql <"$migration" >/dev/null
migration_installed=1
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_reviewed_stage_admission")" \
  == 0 ]]

phase=dry_run
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner" >"$dry"
jq -e '.apply==false and .database_writes==0 and
  .plans[0].route=="admit_reviewed_stage" and .claims==0 and
  .qdrant==0 and .prompt_influence==0 and .external_model_calls==0' \
  "$dry" >/dev/null

phase=transactional_apply
MEMORY_V1_V5_REVIEWED_STAGE_ADMISSION_APPLY=memory_v1_v5_reviewed_stage_admission_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner" --apply >"$applied"
jq -e '.apply==true and .outcome=="reviewed_stage_admitted" and
  .database_rows_created==2 and .zero_write_replay_proved==true and
  .claims==0 and .qdrant==0 and .prompt_influence==0 and
  .external_model_calls==0 and .local_model_calls==0' "$applied" >/dev/null
writes_committed=1

phase=entailment_handoff
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  venv/bin/python "$entailment" --owner-user-id "$owner" >"$entailment_dry"
jq -e '.apply==false and .database_writes==0 and
  .plans[0].route=="private_entailment_assessment" and
  .external_model_calls==0 and .local_model_calls==0 and
  .claim_writes==0 and .qdrant_writes==0 and .prompt_influence==0' \
  "$entailment_dry" >/dev/null

phase=verification
capture_state "$after"
cmp -s "$before" "$after"
stage_count_after=$(scalar \
  'SELECT count(*) FROM memory.v5_local_packet_stage_admission')
[[ "$stage_count_after" -eq $((stage_count_before + 1)) ]]
[[ "$(stage_existing_signature)" == "$stage_existing_before" ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_reviewed_stage_admission
  WHERE owner_user_id='$owner'::uuid AND decision='reviewed_entity_stage'
    AND resolution_count=2 AND approved_review_count=1 AND binding_count=4")" \
  == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_reviewed_stage_admission
  WHERE owner_user_id<>'$owner'::uuid")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_stage_admission
  WHERE owner_user_id<>'$owner'::uuid AND decision='reviewed_entity_stage'")" \
  == 0 ]]
[[ "$(scalar "SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
  AND NOT rolinherit FROM pg_roles
  WHERE rolname='memory_v5_reviewed_stage_maintainer'")" == t ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
jq -n \
  --arg contract_version memory_v1_v5_reviewed_stage_production_report_v1 \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg owner_user_id "$owner" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson timer_count "$timer_count" \
  --argjson applied "$(jq -c . "$applied")" \
  --argjson entailment "$(jq -c . "$entailment_dry")" \
  '{contract_version:$contract_version,completed_at:$completed_at,
    target_server:"seebx",head_commit:$head_commit,
    owner_user_id:$owner_user_id,backup:$backup,
    backup_sha256:$backup_sha256,timer_count:$timer_count,
    database_rows_created:2,zero_write_replay_proved:true,
    account_isolation_proved:true,qdrant_unchanged:true,
    qdrant_sha256:$qdrant_sha256,applied:$applied,
    entailment_handoff:$entailment,
    external_model_calls:0,local_model_calls:0,claim_writes:0,
    qdrant_writes:0,prompt_influence:0,
    hard_stop:"before_private_entailment_execution"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf '%s\n' "memory_v1_v5_reviewed_stage_production_apply: PASS"
printf '%s\n' "report=$report"
printf '%s\n' "backup=$backup"
