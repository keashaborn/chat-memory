#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Generates real embeddings for exactly 19 reviewed claims
# and writes them only to an isolated Qdrant collection. Production Postgres,
# memory_claim_v1, retrieval, answer generation, and prompts remain unchanged.

if [[ "${MEMORY_V1_V5_2_REVIEWED_CLAIM_SHADOW_PROJECTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_REVIEWED_CLAIM_SHADOW_PROJECTION=authorized is required' >&2
  exit 1
fi

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
plan=/home/ubuntu/memory-v1-reviews/reviewed-claim-projection-clone-20260730T172334Z-950aa2b01dbd/projection-plan.json
runner="$repo_root/scripts/memory_v1_v5_2_reviewed_claim_projection_shadow.py"
expected_plan_file_sha=60c90a36a39a7cb01810782649f49cb72f2b920e8f2bae16b8176b71d65a59d0
expected_plan_sha=1ae1c4448c5cf6fbe656a4604060e627b50d138d81ad17101be5c9d48765c785
required_ancestor=950aa2b01dbd9a25a8d7662483ce3b6ca1e5c25e
collection=memory_claim_v1_shadow_reviewed_1ae1c4448c5c
container=brains-postgres-1
database=memory
python_bin=/opt/chat-memory/venv/bin/python
review_root=/home/ubuntu/memory-v1-reviews
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_reviewed_claim_shadow_projection.lock
artifact_dir="$review_root/reviewed-claim-projection-shadow-$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$repo_root" rev-parse --short=12 HEAD)"
complete=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(sha256sum "$plan" | awk '{print $1}')" == "$expected_plan_file_sha" ]]
[[ "$(jq -er '.plan_sha256' "$plan")" == "$expected_plan_sha" ]]
[[ "$(jq -er '.required_ancestor_commit' "$plan")" == "$required_ancestor" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(jq -er '.items|length' "$plan")" == 19 ]]
[[ "$(jq -er '.projection.maximum_embedding_requests' "$plan")" == 19 ]]
[[ "$(jq -er '.projection.automatic_http_retries' "$plan")" == 0 ]]
[[ "$(jq -er '.projection.answer_generation' "$plan")" == false ]]
[[ "$(jq -er '.projection.prompt_influence' "$plan")" == false ]]

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${QDRANT_URL:-}" && -n "${OPENAI_API_KEY:-}" ]]
[[ "${EMBED_MODEL:-text-embedding-3-large}" == text-embedding-3-large ]]
exec 9>"$lock_file"
flock -n 9
umask 077
install -d -m 0700 "$artifact_dir"

cleanup() {
  if [[ "$complete" -ne 1 ]]; then
    curl --silent --show-error --max-time 30 -X DELETE \
      "$QDRANT_URL/collections/$collection" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    "$QDRANT_URL/collections/memory_claim_v1/points/scroll" \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_targets() {
  local output=$1 claim_csv
  claim_csv=$(jq -r '[.items[].claim_id]|join(",")' "$plan")
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "
    SELECT source || E'\t' || row_json
    FROM (
      SELECT 'claim' AS source,to_jsonb(value)::text AS row_json
      FROM memory.claim AS value
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
        AND claim_id=ANY(string_to_array('$claim_csv',',')::uuid[])
      UNION ALL
      SELECT 'claim_revision',to_jsonb(value)::text
      FROM memory.claim_revision AS value
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
        AND claim_id=ANY(string_to_array('$claim_csv',',')::uuid[])
      UNION ALL
      SELECT 'projection_outbox',to_jsonb(value)::text
      FROM memory.projection_outbox AS value
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
        AND aggregate_id=ANY(string_to_array('$claim_csv',',')::uuid[])
    ) AS exact_rows
    ORDER BY source,row_json
  " >"$output"
  chmod 0600 "$output"
}

[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(curl --fail --silent --max-time 5 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -er '.status')" == ok ]]
[[ "$(curl --silent --show-error --max-time 30 -o /dev/null -w '%{http_code}' \
  "$QDRANT_URL/collections/$collection")" == 404 ]]

targets_before="$artifact_dir/production-targets-before.tsv"
targets_after="$artifact_dir/production-targets-after.tsv"
capture_targets "$targets_before"
qdrant_before=$(qdrant_signature)

apply="$artifact_dir/shadow-apply.json"
replay="$artifact_dir/shadow-replay.json"
MEMORY_V1_REQUIRED_ANCESTOR="$required_ancestor" \
MEMORY_V1_V5_2_REVIEWED_CLAIM_SHADOW_PROJECTION=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$runner" --mode apply --plan "$plan" \
  --collection "$collection" --output "$apply"
[[ "$(jq -er '.mode' "$apply")" == apply ]]
[[ "$(jq -er '.claim_count' "$apply")" == 19 ]]
[[ "$(jq -er '.embedding_requests' "$apply")" == 19 ]]
[[ "$(jq -er '.automatic_http_retries' "$apply")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$apply")" == 19 ]]
[[ "$(jq -er '.production_collection_writes' "$apply")" == 0 ]]
[[ "$(jq -er '.database_writes' "$apply")" == 0 ]]
[[ "$(jq -er '[.shadow_tests[]|select(
  .selected_count>=1 and .other_owner_database_record_count==0 and
  .prompt_influence==false)]|length' "$apply")" == 19 ]]

unset OPENAI_API_KEY
MEMORY_V1_REQUIRED_ANCESTOR="$required_ancestor" \
MEMORY_V1_V5_2_REVIEWED_CLAIM_SHADOW_REPLAY=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$runner" --mode replay --plan "$plan" \
  --collection "$collection" --output "$replay"
[[ "$(jq -er '.mode' "$replay")" == replay ]]
[[ "$(jq -er '.embedding_requests' "$replay")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$replay")" == 0 ]]
[[ "$(jq -er '[.shadow_tests[]|select(
  .selected_count>=1 and .other_owner_database_record_count==0 and
  .prompt_influence==false)]|length' "$replay")" == 19 ]]

capture_targets "$targets_after"
cmp -s "$targets_before" "$targets_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(curl --fail --silent --max-time 5 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -er '.status')" == ok ]]

report="$artifact_dir/report.json"
jq -n \
  --arg head "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg plan_sha256 "$expected_plan_sha" \
  --arg plan_file_sha256 "$expected_plan_file_sha" \
  --arg collection "$collection" \
  --arg apply_sha256 "$(sha256sum "$apply" | awk '{print $1}')" \
  --arg replay_sha256 "$(sha256sum "$replay" | awk '{print $1}')" \
  --arg live_qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:"memory_v1_v5_2_reviewed_claim_projection_shadow_report_v1",
    head_commit:$head,
    plan_sha256:$plan_sha256,
    plan_file_sha256:$plan_file_sha256,
    isolated_collection:$collection,
    shadow_apply_file_sha256:$apply_sha256,
    shadow_replay_file_sha256:$replay_sha256,
    claims_embedded:19,
    external_embedding_requests:19,
    automatic_http_retries:0,
    answer_generation:false,
    production_postgres_writes:0,
    production_collection_writes:0,
    production_targets_unchanged:true,
    live_qdrant_sha256:$live_qdrant_sha256,
    live_qdrant_unchanged:true,
    owner_shadow_tests_passed:19,
    cross_owner_isolation:true,
    replay_embedding_requests:0,
    replay_writes:0,
    prompt_influence:false,
    collection_retained_for_controlled_live_activation:true
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
complete=1
printf 'report=%s\n' "$report"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'isolated_collection=%s\n' "$collection"
printf 'memory_v1_v5_2_reviewed_claim_projection_shadow: PASS\n'
