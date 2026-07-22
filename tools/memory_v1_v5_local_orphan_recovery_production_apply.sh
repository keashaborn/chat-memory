#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the owner-scoped orphan recovery function and
# terminalizes exactly three hash-bound expired local inference jobs.

if [[ "${MEMORY_V1_V5_LOCAL_ORPHAN_RECOVERY_RUN:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LOCAL_ORPHAN_RECOVERY_RUN=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-$repo_root/.env}"
set +a

container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
target_job=4febf61b-5139-4eff-939e-45f4d0dff90d
reservation=ae386c15-c6bd-4945-8da3-83b13b745ad2
content=bf129eb2d5f7684502daa89c948da39ca4f7351675187a1bee7ebc43cdbbe663
migration=ops/sql/20260722_memory_v1_v5_local_orphan_recovery.sql
rollback=ops/sql/20260722_memory_v1_v5_local_orphan_recovery_rollback.sql
test_sql=tests/memory_v1_v5_local_orphan_recovery.sql
manifest=ops/sql/20260722_memory_v1_v5_local_orphan_recovery_manifest.sql
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_orphan_recovery.lock

declare -A expected_sha256=(
  ["$migration"]="1199c504c4d47bf4de5ec9b6b7c7dfcd7939014e40301b8d24e57c176d50622c"
  ["$rollback"]="6b21b3469f40be24b190f408a5e552cf543a954cf8386f5ed923d8660d83c513"
  ["$test_sql"]="46ea536e5db84241270a8cb6b0269a682f0e73ffb94f5c2425544af28f1495e4"
  ["$manifest"]="188a53f822ddc5bbdd09463589a285fd07c5e681fd51684e4a5e04ecf7b12b73"
)

timer_state=$(mktemp /tmp/memory-v5-orphan-timers.XXXXXX)
apply_output=$(mktemp /tmp/memory-v5-orphan-apply.XXXXXX)
replay_output=$(mktemp /tmp/memory-v5-orphan-replay.XXXXXX)
chmod 0600 "$timer_state" "$apply_output" "$replay_output"
timers_quiesced=0
phase=initialization

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

cleanup() {
  rc=$?
  trap - EXIT
  restore_timers || rc=1
  rm -f "$timer_state" "$apply_output" "$replay_output"
  if [[ "$rc" -ne 0 ]]; then
    echo "memory_v1_v5_local_orphan_recovery failed at phase=$phase" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

owner_signature() {
  local table=$1 actor=$2
  scalar "SELECT count(*)::text || ':' ||
    encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
      ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM (SELECT to_jsonb(value)::text AS row_json
      FROM memory.\"$table\" AS value
      WHERE owner_user_id='$actor'::uuid) rows"
}

for artifact in "${!expected_sha256[@]}"; do
  [[ -f "$artifact" ]]
  [[ "$(sha256sum "$artifact" | awk '{print $1}')" == \
    "${expected_sha256[$artifact]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor 581ad0d HEAD

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"

phase=quiesce_non_scheduler_timers
while IFS= read -r unit; do
  [[ "$unit" == memory-v1-v5-local-inference-scheduler.timer ]] && continue
  printf '%s\t%s\t%s\n' "$unit" "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
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
[[ "$(systemctl is-active memory-v1-v5-local-inference-scheduler.timer)" == active ]]

phase=fresh_backup
partial="$snapshot_dir/.memory_pre_v5_local_orphan_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_local_orphan_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
jobs_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')
events_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')
local_before=$(scalar 'SELECT count(*) FROM memory.v5_local_inference_event')
packets_before=$(owner_signature evidence_extraction_packet_v5_local "$owner")
claims_before=$(owner_signature claim "$owner")
other_jobs_before=$(owner_signature evidence_extraction_job "$other")
other_events_before=$(owner_signature evidence_extraction_event "$other")
other_local_before=$(owner_signature v5_local_inference_event "$other")
qdrant_before=$(qdrant_signature)
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND status='processing'
    AND lease_expires_at<clock_timestamp() AND job_id IN
      ('4febf61b-5139-4eff-939e-45f4d0dff90d','8e00f329-d359-4c3c-9ce5-6a6ac5155277','395e8ffc-a4fc-41a6-a33c-de5c6be0f36c')")" == 3 ]]

phase=install_additive_function
run_sql <"$migration" >/dev/null
run_sql <"$migration" >/dev/null

phase=rollback_security
run_sql -v target_owner="$owner" -v other_owner="$other" \
  -v target_job="$target_job" -v reservation_event="$reservation" \
  -v content_sha256="$content" <"$test_sql" >/dev/null

phase=transactional_recovery
run_sql <"$manifest" >"$apply_output"
[[ "$(grep -c 'applied' "$apply_output")" -eq 3 ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')" == \
  "$jobs_before" ]]
[[ $(( $(scalar 'SELECT count(*) FROM memory.evidence_extraction_event') - events_before )) -eq 3 ]]
[[ $(( $(scalar 'SELECT count(*) FROM memory.v5_local_inference_event') - local_before )) -eq 3 ]]

phase=zero_write_replay
events_applied=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')
local_applied=$(scalar 'SELECT count(*) FROM memory.v5_local_inference_event')
run_sql <"$manifest" >"$replay_output"
[[ "$(grep -c 'replayed' "$replay_output")" -eq 3 ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" == \
  "$events_applied" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_inference_event')" == \
  "$local_applied" ]]

phase=postconditions
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND status='skipped'
    AND last_error='local_worker_abandoned' AND job_id IN
      ('4febf61b-5139-4eff-939e-45f4d0dff90d','8e00f329-d359-4c3c-9ce5-6a6ac5155277','395e8ffc-a4fc-41a6-a33c-de5c6be0f36c')")" == 3 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$owner'::uuid AND operation_id IN
    ('6eb44d73-582e-5cd4-baa7-c8d3962307fd','98a4bd91-cfc6-5bf1-aeb5-cc8c0aa652ef','3f6a78de-c1eb-5aae-8eec-ac259d79f9cf')")" == 3 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_inference_event
  WHERE owner_user_id='$owner'::uuid AND operation_id IN
    ('4771f9a9-dbae-5680-9a3e-98cf68c9e100','ad6a962e-7345-50e1-8f2d-d5a97e948d34','72191075-18f6-5497-b062-d2e58ea0b79e')
    AND outcome='rejected' AND rejection_code='local_worker_abandoned'
    AND local_model_calls=1 AND external_model_calls=0")" == 3 ]]
[[ "$(owner_signature evidence_extraction_packet_v5_local "$owner")" == \
  "$packets_before" ]]
[[ "$(owner_signature claim "$owner")" == "$claims_before" ]]
[[ "$(owner_signature evidence_extraction_job "$other")" == \
  "$other_jobs_before" ]]
[[ "$(owner_signature evidence_extraction_event "$other")" == \
  "$other_events_before" ]]
[[ "$(owner_signature v5_local_inference_event "$other")" == \
  "$other_local_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=restore_timers
restore_timers
[[ "$(systemctl is-active memory-v1-v5-local-inference-scheduler.timer)" == active ]]
[[ "$(systemctl is-active brains.service)" == active ]]
curl --fail --silent --show-error --max-time 10 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
curl --fail --silent --show-error --max-time 10 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/readyz \
  | jq -e '.ok==true and .postgres==true' >/dev/null

phase=complete
printf '%s\n' "memory_v1_v5_local_orphan_recovery: PASS backup=$backup"
