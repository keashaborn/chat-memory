#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Completes exactly one already-materialized, pending,
# controlled claim projection after a fail-closed projector compatibility stop.

if [[ "${MEMORY_V1_CLAIM_PROJECTION_RECOVERY:-}" != authorized ]]; then
  echo 'MEMORY_V1_CLAIM_PROJECTION_RECOVERY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 4 ]]; then
  echo 'usage: recovery.sh APPLY_RESULT PROJECT_RESULT CLAIM_ID OUTBOX_ID' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
apply_result=$(realpath "$1")
project_result=$(realpath -m "$2")
claim_id=$3
outbox_id=$4
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
runner=scripts/memory_v1_v5_claim_projection_controlled_project.py
review_root=/home/ubuntu/memory-v1-reviews
lock_file=/home/ubuntu/brains/.memory_v1_claim_projection_recovery.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
brains_quiesced=0
brains_state_before=
unit_state=$(mktemp /tmp/memory-v1-project-recovery-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-project-recovery-tables.XXXXXX)

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

restore_runtime() {
  if [[ "$brains_quiesced" -eq 1 ]]; then
    if [[ "$brains_state_before" == active ]]; then
      sudo -n systemctl start brains.service
    else
      sudo -n systemctl stop brains.service
    fi
    brains_quiesced=0
  fi
  if [[ "$units_quiesced" -eq 1 ]]; then
    while IFS=$'\t' read -r unit enabled active; do
      if [[ "$active" == active ]]; then
        sudo -n systemctl start "$unit"
      else
        sudo -n systemctl stop "$unit"
      fi
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      [[ "$(systemctl is-active "$unit")" == "$active" ]]
    done <"$unit_state"
    units_quiesced=0
  fi
}

record_exit() {
  code=$?
  if [[ "$code" -eq 0 && "$phase" != complete ]]; then code=1; fi
  restore_runtime || code=1
  rm -f "$unit_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    printf 'run_tag=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$run_tag" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
}
trap record_exit EXIT

capture_non_outbox_state() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    state=$(psql_row "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),
        'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
            FROM memory.\"$table\" AS value) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

[[ "$apply_result" == "$review_root"/* && -f "$apply_result" ]]
[[ "$(stat -c '%a' "$apply_result")" == 600 ]]
[[ "$project_result" == "$review_root"/* && "$project_result" == *.json && ! -e "$project_result" ]]
[[ "$claim_id" =~ ^[0-9a-f-]{36}$ && "$outbox_id" =~ ^[0-9a-f-]{36}$ ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(jq -er '.owner_user_id' "$apply_result")" == "$owner" ]]
[[ "$(jq -er '.outcomes|length' "$apply_result")" == 1 ]]
[[ "$(jq -er '.outcomes[0].claim_id' "$apply_result")" == "$claim_id" ]]
[[ "$(jq -er '.outcomes[0].outbox_id' "$apply_result")" == "$outbox_id" ]]
[[ "$(jq -er '.outcomes[0].predicate' "$apply_result")" == life_event.died ]]
[[ "$(psql_row "SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id='$owner'::uuid AND outbox_id='$outbox_id'::uuid AND aggregate_id='$claim_id'::uuid AND status='pending' AND attempts=0")" == 1 ]]
[[ "$(psql_row "SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'::uuid AND claim_id='$claim_id'::uuid AND predicate='life_event.died' AND status='supported'")" == 1 ]]
[[ "$(curl --fail --silent --show-error --max-time 30 -H 'content-type: application/json' -d "{\"ids\":[\"$claim_id\"],\"with_payload\":true,\"with_vector\":false}" http://127.0.0.1:6333/collections/memory_claim_v1/points | jq '.result|length')" == 0 ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_claim_projection_recovery_${run_tag}.status"
before_state="$snapshot_dir/memory_v1_claim_projection_recovery_before_${run_tag}.tsv"
after_state="$snapshot_dir/memory_v1_claim_projection_recovery_after_${run_tag}.tsv"

phase=quiesce
: >"$unit_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager | awk '{print $1}' | sort -u)
chmod 0600 "$unit_state"
while IFS=$'\t' read -r unit _enabled active; do
  [[ "$active" != active ]] || sudo -n systemctl stop "$unit"
done <"$unit_state"
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do systemctl is-active --quiet "$service" || break; sleep 1; done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"
brains_state_before=$(systemctl is-active brains.service)
[[ "$brains_state_before" != active ]] || sudo -n systemctl stop brains.service
brains_quiesced=1
for _attempt in $(seq 1 30); do systemctl is-active --quiet brains.service || break; sleep 1; done
! systemctl is-active --quiet brains.service

phase=backup
backup_partial="$snapshot_dir/.memory_pre_claim_projection_recovery_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_claim_projection_recovery_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -X -A -t -U sage -d "$database" -c "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name <> 'projection_outbox' ORDER BY table_name" >"$table_list"
capture_non_outbox_state "$before_state"

phase=controlled_projection
set -a
source "$repo_root/.env"
set +a
MEMORY_V1_CONTROLLED_PROJECTION=authorized PYTHONPATH="$repo_root:$repo_root/scripts" \
  "$repo_root/venv/bin/python" "$repo_root/$runner" \
  --apply-result "$apply_result" --output "$project_result"
[[ "$(jq -er '.embedding_requests' "$project_result")" == 1 ]]
[[ "$(jq -er '.automatic_http_retries' "$project_result")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$project_result")" == 1 ]]
[[ "$(jq -er '.shadow_tests|length' "$project_result")" == 1 ]]
[[ "$(jq -er '.shadow_tests[0].other_owner_candidate_count' "$project_result")" == 0 ]]
[[ "$(psql_row "SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id='$owner'::uuid AND outbox_id='$outbox_id'::uuid AND status='done' AND attempts=1")" == 1 ]]
[[ "$(curl --fail --silent --show-error --max-time 30 -H 'content-type: application/json' -d "{\"ids\":[\"$claim_id\"],\"with_payload\":true,\"with_vector\":false}" http://127.0.0.1:6333/collections/memory_claim_v1/points | jq '.result|length')" == 1 ]]
capture_non_outbox_state "$after_state"
cmp -s "$before_state" "$after_state"

phase=restore_runtime
restore_runtime
for _attempt in $(seq 1 30); do
  if [[ "$(systemctl is-active brains.service)" == active ]] \
     && curl --fail --silent --max-time 5 -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
        http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null \
     && curl --fail --silent --max-time 5 -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
        http://127.0.0.1:8088/readyz | jq -e '.ok==true and .postgres==true' >/dev/null; then
    break
  fi
  sleep 1
done
[[ "$(systemctl is-active brains.service)" == active ]]

phase=report
report="$snapshot_dir/memory_v1_claim_projection_recovery_${run_tag}.json"
jq -n --arg head "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg backup "$backup" --arg result "$project_result" --arg claim_id "$claim_id" \
  --arg before "$before_state" --arg after "$after_state" \
  '{contract_version:"memory_v1_claim_projection_recovery_report_v1",
    head_commit:$head,backup:$backup,project_result:$result,claim_id:$claim_id,
    checks:{one_embedding_request:true,zero_http_retries:true,one_qdrant_write:true,
      outbox_done_once:true,cross_owner_shadow_clear:true,
      all_non_outbox_memory_tables_unchanged:true,runtime_restored:true},
    evidence:{before:$before,after:$after}}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_claim_projection_recovery: PASS\nbackup=%s\nreport=%s\nresult=%s\n' \
  "$backup" "$report" "$project_result"
