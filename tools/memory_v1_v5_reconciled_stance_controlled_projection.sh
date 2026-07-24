#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Projects exactly one admitted reconciled stance claim to
# Qdrant and runs owner-only shadow retrieval. It does not activate prompt or
# answer influence.

if [[ "${MEMORY_V1_RECONCILED_STANCE_CONTROLLED_PROJECTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_RECONCILED_STANCE_CONTROLLED_PROJECTION=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=e8eaa93ff96d9cce6ffaf3e2852c2070bb0df7e4
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
claim_id=8fb8b3ab-a627-4555-99c6-fe4dc9b0ca89
apply_result=/home/ubuntu/memory-v1-reviews/reconciled-production-20260724T185638Z_ab5a83e9c3f6/materialize-apply.json
admission_result=/home/ubuntu/memory-v1-reviews/deferred-projection-production-20260724T190850Z_e8eaa93ff96d/apply.json
runner=scripts/memory_v1_v5_claim_projection_controlled_project.py
python_bin="$repo_root/venv/bin/python"
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_reconciled_stance_projection.lock
phase=initialization
timers_quiesced=0
brains_quiesced=0
brains_state_before=
run_id=
artifact_dir=
status_file=
timer_state=$(mktemp /tmp/memory-v1-reconciled-project-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-reconciled-project-tables.XXXXXX)

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

qdrant_claim_count() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d "{\"ids\":[\"$claim_id\"],\"with_payload\":true,\"with_vector\":false}" \
    http://127.0.0.1:6333/collections/memory_claim_v1/points \
    | jq -er '.result|length'
}

qdrant_non_target_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS --arg claim "$claim_id" \
        '.result.points | map(select((.id|tostring)!=$claim)) | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

authenticated_health() {
  set -a
  source "$repo_root/.env"
  set +a
  for _attempt in $(seq 1 30); do
    if [[ "$(systemctl is-active brains.service)" == active ]] \
       && curl --fail --silent --max-time 5 \
          -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
          http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null \
       && curl --fail --silent --max-time 5 \
          -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
          http://127.0.0.1:8088/readyz | jq -e '.ok==true and .postgres==true' >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

restore_runtime() {
  if [[ "$brains_quiesced" -eq 1 ]]; then
    if [[ "$brains_state_before" == active ]]; then
      sudo -n systemctl start brains.service
    else
      sudo -n systemctl stop brains.service
    fi
    [[ "$(systemctl is-active brains.service)" == "$brains_state_before" ]]
    brains_quiesced=0
  fi
  if [[ "$timers_quiesced" -eq 1 ]]; then
    while IFS=$'\t' read -r unit enabled active; do
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      if [[ "$active" == active ]]; then
        sudo -n systemctl start "$unit"
      else
        sudo -n systemctl stop "$unit"
      fi
      [[ "$(systemctl is-active "$unit")" == "$active" ]]
    done <"$timer_state"
    timers_quiesced=0
  fi
}

record_exit() {
  code=$?
  if [[ "$code" -eq 0 && "$phase" != complete ]]; then code=1; fi
  restore_runtime || code=1
  rm -f "$timer_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    printf 'run_id=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$run_id" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
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
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
head=$(git -C "$repo_root" rev-parse HEAD)
[[ -x "$python_bin" && -f "$repo_root/$runner" ]]
[[ -f "$apply_result" && -f "$admission_result" ]]
[[ "$(stat -c '%a' "$apply_result")" == 600 ]]
[[ "$(stat -c '%a' "$admission_result")" == 600 ]]
[[ "$(jq -er '.result_sha256' "$apply_result")" == \
  c9d48e3c608c8ff3138b175c178c29d565dc5dd3435ae5d669431daf6a7d914d ]]
[[ "$(jq -er '.result_sha256' "$admission_result")" == \
  6ae5c433a9c3617c6d2c519152fa0c46f9bf2b15f001e386b072ee5d005a6e7d ]]
outbox_id=$(jq -er '.outcomes[0].outbox_id' "$admission_result")
[[ "$(psql_row "
  SELECT count(*) FROM memory.projection_outbox
  WHERE owner_user_id='$owner'::uuid
    AND outbox_id='$outbox_id'::uuid
    AND aggregate_id='$claim_id'::uuid
    AND status='pending' AND attempts=0
")" == 1 ]]
[[ "$(qdrant_claim_count)" == 0 ]]

set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${OPENAI_API_KEY:-}" ]]
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}"
artifact_dir="$review_root/reconciled-projection-production-$run_id"
status_file="$snapshot_root/memory_v1_reconciled_stance_projection_${run_id}.status"
mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"

phase=quiesce
: >"$timer_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
chmod 0600 "$timer_state"
while IFS=$'\t' read -r unit _enabled active; do
  [[ "$active" != active ]] || sudo -n systemctl stop "$unit"
done <"$timer_state"
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"
brains_state_before=$(systemctl is-active brains.service)
[[ "$brains_state_before" != active ]] || sudo -n systemctl stop brains.service
brains_quiesced=1
for _attempt in $(seq 1 30); do
  systemctl is-active --quiet brains.service || break
  sleep 1
done
! systemctl is-active --quiet brains.service

phase=backup
backup_partial="$snapshot_root/.memory_pre_reconciled_projection_${run_id}.dump.partial"
backup="$snapshot_root/memory_pre_reconciled_projection_${run_id}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -X -A -t -U sage -d "$database" -c "
  SELECT table_name
  FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name <> 'projection_outbox'
  ORDER BY table_name
" >"$table_list"
before="$artifact_dir/non-outbox-before.tsv"
after="$artifact_dir/non-outbox-after.tsv"
capture_non_outbox_state "$before"
qdrant_non_target_before=$(qdrant_non_target_signature)

phase=controlled_projection
project_result="$artifact_dir/project-result.json"
MEMORY_V1_CONTROLLED_PROJECTION=authorized \
PYTHONPATH="$repo_root:$repo_root/scripts" \
  "$python_bin" "$repo_root/$runner" \
  --apply-result "$apply_result" \
  --admission-result "$admission_result" \
  --output "$project_result"
[[ "$(jq -er '.embedding_requests' "$project_result")" == 1 ]]
[[ "$(jq -er '.automatic_http_retries' "$project_result")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$project_result")" == 1 ]]
[[ "$(jq -er '.shadow_tests|length' "$project_result")" == 1 ]]
[[ "$(jq -er '.shadow_tests[0].predicate' "$project_result")" == stance.reported ]]
[[ "$(jq -er '.shadow_tests[0].other_owner_candidate_count' "$project_result")" == 0 ]]
[[ "$(jq -er '.shadow_tests[0].selected_count' "$project_result")" -ge 1 ]]
[[ "$(jq -er '.prompt_influence_activated' "$project_result")" == false ]]

phase=verify
[[ "$(psql_row "
  SELECT count(*) FROM memory.projection_outbox
  WHERE owner_user_id='$owner'::uuid
    AND outbox_id='$outbox_id'::uuid
    AND aggregate_id='$claim_id'::uuid
    AND status='done' AND attempts=1
")" == 1 ]]
[[ "$(qdrant_claim_count)" == 1 ]]
qdrant_owner=$(
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d "{\"ids\":[\"$claim_id\"],\"with_payload\":true,\"with_vector\":false}" \
    http://127.0.0.1:6333/collections/memory_claim_v1/points \
    | jq -er '.result[0].payload.owner_user_id'
)
[[ "$qdrant_owner" == "$owner" ]]
qdrant_non_target_after=$(qdrant_non_target_signature)
[[ "$qdrant_non_target_after" == "$qdrant_non_target_before" ]]
capture_non_outbox_state "$after"
cmp -s "$before" "$after"

phase=restore_runtime
restore_runtime
authenticated_health

phase=report
report="$artifact_dir/report.json"
jq -n \
  --arg head "$head" --arg backup "$backup" \
  --arg claim_id "$claim_id" --arg outbox_id "$outbox_id" \
  --arg project_result "$project_result" \
  --arg result_sha256 "$(jq -er '.result_sha256' "$project_result")" \
  --arg qdrant_non_target_sha256 "$qdrant_non_target_after" \
  '{
    contract_version:"memory_v1_v5_reconciled_stance_controlled_projection_report_v1",
    head_commit:$head,
    owner_user_id:"1240822d-ac9a-4096-95aa-e2b24d36ef50",
    backup:$backup,
    claim_id:$claim_id,
    outbox_id:$outbox_id,
    project_result:$project_result,
    project_result_sha256:$result_sha256,
    embedding_requests:1,
    automatic_http_retries:0,
    qdrant_writes:1,
    qdrant_target_points:1,
    qdrant_owner_verified:true,
    qdrant_non_target_sha256:$qdrant_non_target_sha256,
    qdrant_non_target_unchanged:true,
    outbox_done_once:true,
    cross_owner_candidate_count:0,
    shadow_selected_count:1,
    non_outbox_memory_unchanged:true,
    timers_restored_exactly:true,
    brains_service_restored:true,
    prompt_influence_activated:false,
    general_account_activation:false
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'backup=%s\nreport=%s\nproject_result=%s\n' \
  "$backup" "$report" "$project_result"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'memory_v1_v5_reconciled_stance_controlled_projection: PASS\n'
