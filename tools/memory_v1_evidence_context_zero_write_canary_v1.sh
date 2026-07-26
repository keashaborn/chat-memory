#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Runs one private local model call over the exact
# owner-scoped sibling-context envelope and makes no persistent data writes.

if [[ ${EUID} -ne 0 ]]; then
  echo 'run through sudo; root is required for the private endpoint key' >&2
  exit 2
fi
if [[ "${MEMORY_V1_EVIDENCE_CONTEXT_CANARY_AUTHORIZED:-}" != authorized ]]; then
  echo 'explicit contextual canary authorization is required' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
target=049205b4-9a6c-5e1a-bb8f-2ab9f05f8964
target_sha=579427858fe9e181b02fc4c622a56cdda4f75e86356baf91664504a16396899d
container=brains-postgres-1
database=memory
env_file=/opt/chat-memory/.env
api_key_file=/etc/memory-v1-local-inference/api-key
run_tag=$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)
review_dir=/home/ubuntu/memory-v1-reviews/evidence-context-canary-"$run_tag"
mkdir -p "$review_dir"
chmod 0700 "$review_dir"
report="$review_dir/report.json"
packet="$review_dir/packet.json"
unit_state="$review_dir/timers.tsv"
before="$review_dir/state-before.tsv"
after="$review_dir/state-after.tsv"
touch "$unit_state" "$before" "$after"
chmod 0600 "$unit_state" "$before" "$after"

timers_restored=0
restore_timers() {
  if [[ "$timers_restored" == 1 || ! -s "$unit_state" ]]; then
    return
  fi
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    else
      systemctl disable "$unit" >/dev/null 2>&1 || true
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
  done <"$unit_state"
  timers_restored=1
}
cleanup() {
  rc=$?
  trap - EXIT
  restore_timers
  exit "$rc"
}
trap cleanup EXIT

capture_state() {
  docker exec -i "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 <<SQL
SELECT 'target_evidence',count(*),max(content_sha256)
FROM memory.evidence
WHERE owner_user_id='$owner'::uuid AND evidence_id='$target'::uuid
UNION ALL
SELECT 'extraction_jobs',count(*),max(updated_at)::text
FROM memory.evidence_extraction_job
UNION ALL
SELECT 'extraction_events',count(*),max(created_at)::text
FROM memory.evidence_extraction_event
UNION ALL
SELECT 'local_events',count(*),max(created_at)::text
FROM memory.v5_local_inference_event
UNION ALL
SELECT 'local_packets',count(*),max(created_at)::text
FROM memory.evidence_extraction_packet_v5_local
UNION ALL
SELECT 'claims',count(*),max(created_at)::text
FROM memory.claim
UNION ALL
SELECT 'observations',count(*),max(created_at)::text
FROM memory.observation
UNION ALL
SELECT 'preferences',count(*),max(created_at)::text
FROM memory.preference_head_v5
UNION ALL
SELECT 'project_knowledge',count(*),max(created_at)::text
FROM memory.project_knowledge_head
UNION ALL
SELECT 'project_knowledge_v5',count(*),max(created_at)::text
FROM memory.project_knowledge_head_v5
ORDER BY 1;
SQL
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

[[ -r "$env_file" && -r "$api_key_file" ]]
[[ "$(stat -c %a "$api_key_file")" == 600 ]]
[[ -z "$(git status --short)" ]]
reserved=$(docker exec "$container" psql -U sage -d "$database" -X -Atqc "
  SELECT count(*)
  FROM memory.v5_local_inference_event
  WHERE action='reserved'
    AND created_at>=clock_timestamp()-interval '24 hours'")
[[ "$reserved" =~ ^[0-9]+$ && "$reserved" -le 10 ]]

systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u \
  | while read -r unit; do
      printf '%s\t%s\t%s\n' "$unit" \
        "$(systemctl is-enabled "$unit")" \
        "$(systemctl is-active "$unit")"
    done >"$unit_state"
[[ -s "$unit_state" ]]
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$unit_state"

capture_state >"$before"
qdrant_before=$(qdrant_signature)
set -a
source "$env_file"
set +a
set +e
MEMORY_V1_EVIDENCE_CONTEXT_CANARY=memory_v1_evidence_context_zero_write_canary_v1 \
MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(<"$api_key_file")" \
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/tools/memory_v1_evidence_context_local_canary_v1.py" \
  --owner-user-id "$owner" \
  --target-evidence-id "$target" \
  --expected-target-content-sha256 "$target_sha" \
  --packet-output "$packet" >"$report"
canary_rc=$?
set -e
chmod 0600 "$report"
capture_state >"$after"
[[ -s "$before" && -s "$after" ]]
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
restore_timers
[[ "$(systemctl is-active brains.service)" == active ]]

[[ "$canary_rc" == 0 ]]
jq -e '
  .outcome=="accepted"
  and .external_model_calls==0
  and .local_model_calls==1
  and .write_counts=={
    "database":0,"prompt_influence":0,"qdrant":0,"retrieval":0
  }
' "$report" >/dev/null
[[ -s "$packet" && "$(stat -c %a "$packet")" == 600 ]]

printf '%s\n' \
  'memory_v1_evidence_context_zero_write_canary_v1: PASS' \
  "report=$report" \
  "packet=$packet"
