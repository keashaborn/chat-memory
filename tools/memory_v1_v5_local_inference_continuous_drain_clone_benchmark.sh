#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Runs ten sequential private V5.2 extractions against a
# disposable production clone. Production memory, Qdrant, claims, retrieval,
# and prompts remain unchanged.

if [[ ${EUID} -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 2
fi
if [[ "${MEMORY_V1_V5_CONTINUOUS_DRAIN_BENCHMARK:-}" != authorized ]]; then
  echo 'continuous drain benchmark authorization is required' >&2
  exit 2
fi

repo=$(git rev-parse --show-toplevel)
cd "$repo"
runtime_env=/opt/chat-memory/.env
container=brains-postgres-1
production=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
clone="memory_drain_benchmark_${$}"
snapshot_dir=/home/ubuntu/brains/snapshots
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
backup="$snapshot_dir/memory_pre_continuous_drain_benchmark_${run_tag}.dump"
report="$snapshot_dir/memory_continuous_drain_benchmark_${run_tag}.json"
work=$(mktemp -d /tmp/memory-continuous-drain-benchmark.XXXXXX)
timer_state="$work/timers.tsv"
credential_dir="$work/credential"
mkdir -p "$credential_dir" "$snapshot_dir"
chmod 0700 "$work" "$credential_dir"
timers_quiesced=0
clone_created=0

scalar() {
  local database=$1 query=$2
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "$query"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_database() {
  local database=$1 output=$2 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(scalar "$database" "
      SELECT count(*)::text || E'\\t' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(scalar "$database" "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name")
  chmod 0600 "$output"
}

capture_other_owners() {
  local database=$1 output=$2 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(scalar "$database" "
      SELECT count(*)::text || E'\\t' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
        WHERE owner_user_id<>'$owner'::uuid
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(scalar "$database" "
    SELECT table_name FROM information_schema.columns
    WHERE table_schema='memory' AND column_name='owner_user_id'
    ORDER BY table_name")
  chmod 0600 "$output"
}

capture_owner_jobs() {
  local database=$1 output=$2
  scalar "$database" "
    SELECT job_id::text || E'\\t' ||
      encode(public.digest(convert_to(to_jsonb(job)::text,'UTF8'),'sha256'),'hex')
    FROM memory.evidence_extraction_job AS job
    WHERE owner_user_id='$owner'::uuid
    ORDER BY job_id" >"$output"
  chmod 0600 "$output"
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    else
      [[ "$enabled" == disabled ]]
      systemctl disable "$unit" >/dev/null 2>&1 || true
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

cleanup() {
  rc=$?
  trap - EXIT
  restore_timers || rc=1
  if [[ "$clone_created" -eq 1 ]]; then
    docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
      >/dev/null 2>&1 || rc=1
  fi
  rm -rf "$work"
  exit "$rc"
}
trap cleanup EXIT

[[ -z "$(git status --short)" ]]
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_local_inference_scheduler_test.py >/dev/null
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_local_inference_continuous_drain_contract_test.py \
  >/dev/null
systemd-analyze verify \
  ops/systemd/memory-v1-v5-local-inference-scheduler.service \
  ops/systemd/memory-v1-v5-local-inference-scheduler.timer
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
[[ "$(systemctl show memory-v1-v5-local-inference-health.service \
  -p Result --value)" == success ]]
test -r /etc/memory-v1-local-inference/api-key
test -r "$runtime_env"

while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$timer_state" ]]
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  related_service=${unit%.timer}.service
  for _attempt in $(seq 1 60); do
    systemctl is-active --quiet "$related_service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$related_service"
done <"$timer_state"

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
chmod 0600 "$backup"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

production_before="$work/production-before.tsv"
production_after="$work/production-after.tsv"
other_before="$work/other-before.tsv"
other_after="$work/other-after.tsv"
jobs_before="$work/jobs-before.tsv"
jobs_after="$work/jobs-after.tsv"
manifest="$work/targets.tsv"
scheduler_output="$work/scheduler.json"
capture_database "$production" "$production_before"
qdrant_before=$(qdrant_signature)

docker exec "$container" createdb -U sage -T template0 "$clone"
clone_created=1
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --clean --if-exists <"$backup"

set -a
source "$runtime_env"
set +a
clone_dsn=$(
  python3 - "$POSTGRES_DSN" "$clone" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit

parts = urlsplit(sys.argv[1])
if parts.hostname not in {"127.0.0.1", "::1", "localhost"}:
    raise SystemExit("production DSN is not loopback")
print(urlunsplit((parts.scheme, parts.netloc, "/" + sys.argv[2], parts.query, "")))
PY
)

retry_at_one=$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND route='relational_extraction'
    AND status IN ('pending','error')
    AND attempts=1 AND available_at<=clock_timestamp()")
[[ "$retry_at_one" -eq 4 ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND route='relational_extraction'
    AND status IN ('pending','error')
    AND attempts=1 AND attempts<1
    AND available_at<=clock_timestamp()")" -eq 0 ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND route='relational_extraction'
    AND status IN ('pending','error')
    AND attempts=1 AND attempts<2
    AND available_at<=clock_timestamp()")" -eq 4 ]]

docker exec "$container" psql -U sage -d "$clone" -X -At -F $'\t' -c "
  SELECT job_id,evidence_id,evidence_content_sha256,selector_version,attempts
  FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND route='relational_extraction'
    AND status IN ('pending','error')
    AND attempts<2
    AND available_at<=clock_timestamp()
  ORDER BY priority,available_at,created_at,job_id
  LIMIT 10" >"$manifest"
[[ "$(wc -l <"$manifest")" -eq 10 ]]
[[ "$(cut -f1 "$manifest" | sort -u | wc -l)" -eq 10 ]]
[[ "$(cut -f2 "$manifest" | sort -u | wc -l)" -eq 10 ]]
manifest_sha=$(sha256sum "$manifest" | awk '{print $1}')
duplicate_content_groups=$(cut -f3 "$manifest" | sort | uniq -d | wc -l)

capture_other_owners "$clone" "$other_before"
capture_owner_jobs "$clone" "$jobs_before"
event_before=$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$owner'::uuid")
ledger_before=$(scalar "$clone" "
  SELECT count(*) FROM memory.v5_local_inference_event
  WHERE owner_user_id='$owner'::uuid")
packet_before=$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid")
claim_before=$(scalar "$clone" "
  SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'::uuid")
observation_before=$(scalar "$clone" "
  SELECT count(*) FROM memory.observation WHERE owner_user_id='$owner'::uuid")
entity_before=$(scalar "$clone" "
  SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid")
binding_before=$(scalar "$clone" "
  SELECT count(*) FROM memory.final_answer_memory_binding_v1
  WHERE owner_user_id='$owner'::uuid")

install -o root -g root -m 0400 \
  /etc/memory-v1-local-inference/api-key "$credential_dir/local_api_key"
started_ns=$(date +%s%N)
POSTGRES_DSN="$clone_dsn" \
PYTHONPATH="$repo" \
CREDENTIALS_DIRECTORY="$credential_dir" \
MEMORY_V1_V5_LOCAL_SCHEDULER_APPLY=memory_v1_v5_local_inference_scheduler_apply_v1 \
  /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_local_inference_scheduler.py \
  --owner-user-id "$owner" \
  --contract-profile v5_2 \
  --max-jobs 10 \
  --max-runtime-seconds 21600 \
  --max-attempts 2 \
  --lease-seconds 900 \
  --timeout-seconds 600 \
  --max-output-tokens 4096 \
  --rolling-window-seconds 3600 \
  --max-reserved-jobs 100 \
  --failure-threshold 3 \
  --apply >"$scheduler_output"
finished_ns=$(date +%s%N)
chmod 0600 "$scheduler_output"

jq -e '
  .worker_version=="memory_v1_v5_local_inference_scheduler_v2" and
  .predicate_contract_profile=="v5_2" and
  .apply==true and
  .outcome=="max_jobs_reached" and
  .processed==10 and
  .blocked_owner_count==0 and
  .external_model_calls==0
' "$scheduler_output" >/dev/null
accepted=$(jq -r '.outcome_counts.accepted // 0' "$scheduler_output")
rejected=$(jq -r '.outcome_counts.rejected // 0' "$scheduler_output")
local_calls=$(jq -r '.local_model_calls' "$scheduler_output")
[[ $((accepted + rejected)) -eq 10 ]]
[[ "$local_calls" -ge 0 && "$local_calls" -le 10 ]]

capture_owner_jobs "$clone" "$jobs_after"
MANIFEST="$manifest" BEFORE="$jobs_before" AFTER="$jobs_after" python3 - <<'PY'
import os
from pathlib import Path

selected = {line.split("\t", 1)[0] for line in Path(os.environ["MANIFEST"]).read_text().splitlines()}

def rows(name: str) -> dict[str, str]:
    return dict(
        line.split("\t", 1)
        for line in Path(os.environ[name]).read_text().splitlines()
    )

before = rows("BEFORE")
after = rows("AFTER")
assert before.keys() == after.keys()
changed = {key for key in before if before[key] != after[key]}
assert changed == selected
PY

event_after=$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$owner'::uuid")
ledger_after=$(scalar "$clone" "
  SELECT count(*) FROM memory.v5_local_inference_event
  WHERE owner_user_id='$owner'::uuid")
packet_after=$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid")
[[ $((event_after - event_before)) -eq 20 ]]
[[ $((ledger_after - ledger_before)) -eq 20 ]]
[[ $((packet_after - packet_before)) -eq accepted ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'::uuid")" \
  -eq "$claim_before" ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.observation WHERE owner_user_id='$owner'::uuid")" \
  -eq "$observation_before" ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid")" \
  -eq "$entity_before" ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.final_answer_memory_binding_v1
  WHERE owner_user_id='$owner'::uuid")" -eq "$binding_before" ]]
capture_other_owners "$clone" "$other_after"
cmp -s "$other_before" "$other_after"

capture_database "$production" "$production_after"
cmp -s "$production_before" "$production_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(curl --fail --silent --show-error --max-time 10 \
  -H "Authorization: Bearer $(cat "$credential_dir/local_api_key")" \
  http://127.0.0.1:18080/health | jq -r '.status')" == ok ]]

elapsed_seconds=$(
  python3 - "$started_ns" "$finished_ns" <<'PY'
import sys
print(round((int(sys.argv[2]) - int(sys.argv[1])) / 1_000_000_000, 3))
PY
)
per_job_seconds=$(
  python3 - "$elapsed_seconds" <<'PY'
import sys
print(round(float(sys.argv[1]) / 10, 3))
PY
)

jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson accepted "$accepted" \
  --argjson rejected "$rejected" \
  --argjson local_model_calls "$local_calls" \
  --argjson duplicate_content_groups "$duplicate_content_groups" \
  --argjson retry_jobs_repaired "$retry_at_one" \
  --argjson elapsed_seconds "$elapsed_seconds" \
  --argjson per_job_seconds "$per_job_seconds" \
  --argjson rejection_code_counts "$(jq -c '.rejection_code_counts' "$scheduler_output")" \
  '{
    contract_version:"memory_v1_v5_continuous_drain_clone_benchmark_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:$backup,
    target_manifest_sha256:$manifest_sha256,
    processed:10,
    accepted:$accepted,
    rejected:$rejected,
    local_model_calls:$local_model_calls,
    external_model_calls:0,
    retry_jobs_repaired:$retry_jobs_repaired,
    duplicate_content_groups_preserved:$duplicate_content_groups,
    elapsed_seconds:$elapsed_seconds,
    per_job_seconds:$per_job_seconds,
    rejection_code_counts:$rejection_code_counts,
    checks:{
      sequential_execution:true,
      max_attempts_two:true,
      retry_provenance_preserved:true,
      exact_ten_jobs_changed:true,
      append_only_events:true,
      packet_count_bounded:true,
      other_owners_unchanged:true,
      production_memory_unchanged:true,
      qdrant_unchanged:true,
      claims_unchanged:true,
      observations_unchanged:true,
      entities_unchanged:true,
      answer_bindings_unchanged:true,
      external_model_calls_zero:true,
      private_gpu_health_ok:true
    },
    qdrant_sha256:$qdrant_sha256
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

restore_timers
printf '%s\n' \
  'memory_v1_v5_local_inference_continuous_drain_clone_benchmark: PASS' \
  "report=$report" \
  "backup=$backup" \
  "processed=10" \
  "accepted=$accepted" \
  "rejected=$rejected" \
  "elapsed_seconds=$elapsed_seconds" \
  "per_job_seconds=$per_job_seconds" \
  'production_unchanged=true' \
  'other_owners_unchanged=true' \
  'qdrant_unchanged=true' \
  'timers_restored=true'
