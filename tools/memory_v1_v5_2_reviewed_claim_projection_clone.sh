#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Builds the exact 19-claim read-only production plan, then
# clone-tests outbox admission, deterministic local vectors, owner isolation,
# and zero-write replay. Production Postgres and memory_claim_v1 remain read-only.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
plan_runner="$repo_root/scripts/memory_v1_v5_2_reviewed_claim_projection_plan.py"
clone_runner="$repo_root/scripts/memory_v1_v5_2_reviewed_claim_projection_clone.py"
source_root=/home/ubuntu/memory-v1-reviews/legacy-stage-claim-materialization-production-20260730T170635Z_461b199e23f0
apply_result="$source_root/claim-apply.json"
apply_manifest="$source_root/claim-apply-manifest.json"
container=brains-postgres-1
source_db=memory
head=$(git -C "$repo_root" rev-parse HEAD)
clone_db="memory_v5_reviewed_projection_$(date -u +%Y%m%d%H%M%S)_$$"
collection="memory_claim_v1_clone_reviewed_$(date -u +%Y%m%d%H%M%S)_$$"
python_bin=/opt/chat-memory/venv/bin/python
artifact_dir="/home/ubuntu/memory-v1-reviews/reviewed-claim-projection-clone-$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$repo_root" rev-parse --short=12 HEAD)"
table_list=$(mktemp /tmp/memory-v1-reviewed-projection-tables.XXXXXX)
clone_exists=0
collection_exists=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ -f "$apply_result" && -f "$apply_manifest" ]]
[[ "$(stat -c '%a' "$apply_result")" == 600 ]]
[[ "$(stat -c '%a' "$apply_manifest")" == 600 ]]
install -d -m 0700 "$artifact_dir"

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${QDRANT_URL:-}" ]]

cleanup() {
  if [[ "$collection_exists" -eq 1 ]]; then
    curl --silent --show-error --max-time 30 -X DELETE \
      "$QDRANT_URL/collections/$collection" >/dev/null 2>&1 || true
  fi
  if [[ "$clone_exists" -eq 1 ]]; then
    docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
      >/dev/null 2>&1 || true
  fi
  rm -f "$table_list"
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

capture_memory_without_outbox() {
  local database=$1 output=$2 table state
  : >"$output"
  while IFS= read -r table; do
    state=$(
      docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
        -U sage -d "$database" -c "
        SELECT count(*)::text || E'\t' ||
               encode(public.digest(convert_to(
                 coalesce(string_agg(row_json,E'\n' ORDER BY row_json),''),
                 'UTF8'),'sha256'),'hex')
        FROM (
          SELECT to_jsonb(value)::text AS row_json
          FROM memory.\"$table\" AS value
        ) AS rows
      " | sed -n '1p'
    )
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

capture_production_targets() {
  local output=$1 claim_csv
  claim_csv=$(jq -r '[.items[].claim_id]|join(",")' "$plan")
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$source_db" -c "
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

plan="$artifact_dir/projection-plan.json"
MEMORY_V1_REQUIRED_HEAD="$head" POSTGRES_DSN="$POSTGRES_DSN" \
QDRANT_URL="$QDRANT_URL" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$plan_runner" --apply-result "$apply_result" \
  --apply-manifest "$apply_manifest" --output "$plan"
expected_plan_sha=$(jq -er '.plan_sha256' "$plan")
[[ "$(jq -er '.items|length' "$plan")" == 19 ]]
[[ "$(jq -er '[.items[]|select(.prior_qdrant=="absent")]|length' "$plan")" == 19 ]]
[[ "$(jq -er '[.items[]|select(.prior_outbox=="absent")]|length' "$plan")" == 19 ]]
[[ "$(jq -er '.projection.maximum_embedding_requests' "$plan")" == 19 ]]
[[ "$(jq -er '.required_head_commit' "$plan")" == "$head" ]]

docker exec "$container" psql -X -A -t -U sage -d "$source_db" -c "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name <> 'projection_outbox'
  ORDER BY table_name
" >"$table_list"
[[ -s "$table_list" ]]

production_targets_before="$artifact_dir/production-targets-before.tsv"
production_targets_after="$artifact_dir/production-targets-after.tsv"
clone_memory_before="$artifact_dir/clone-memory-before.tsv"
clone_memory_after="$artifact_dir/clone-memory-after.tsv"
capture_production_targets "$production_targets_before"
qdrant_before=$(qdrant_signature)

docker exec "$container" createdb -U sage -T template0 "$clone_db"
clone_exists=1
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"
clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" "$python_bin" - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

value = urlsplit(os.environ["SOURCE_DSN"])
print(urlunsplit((
    value.scheme,
    value.netloc,
    "/" + os.environ["CLONE_DB"],
    value.query,
    value.fragment,
)))
PY
)
capture_memory_without_outbox "$clone_db" "$clone_memory_before"

result="$artifact_dir/runner-result.json"
collection_exists=1
MEMORY_V1_REQUIRED_HEAD="$head" POSTGRES_DSN="$clone_dsn" \
QDRANT_URL="$QDRANT_URL" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$clone_runner" --plan "$plan" --output "$result" \
  --collection "$collection"

[[ "$(jq -er '.claim_count' "$result")" == 19 ]]
[[ "$(jq -er '.new_projection_count' "$result")" == 19 ]]
[[ "$(jq -er '.replacement_projection_count' "$result")" == 0 ]]
[[ "$(jq -er '.embedding_requests' "$result")" == 19 ]]
[[ "$(jq -er '.external_model_calls' "$result")" == 0 ]]
[[ "$(jq -er '.production_qdrant_writes' "$result")" == 0 ]]
[[ "$(jq -er '.projection_result.claimed' "$result")" == 19 ]]
[[ "$(jq -er '.projection_result.upserted' "$result")" == 19 ]]
[[ "$(jq -er '.projection_result.errors' "$result")" == 0 ]]
[[ "$(jq -er '.replay_result.claimed' "$result")" == 0 ]]
[[ "$(jq -er '.cross_owner.cross_owner_claim_count' "$result")" == 0 ]]
[[ "$(jq -er '.cross_owner.cross_owner_outbox_count' "$result")" == 0 ]]
[[ "$(jq -er '.cross_owner.cross_owner_insert_rejected' "$result")" == true ]]

capture_memory_without_outbox "$clone_db" "$clone_memory_after"
cmp -s "$clone_memory_before" "$clone_memory_after"
capture_production_targets "$production_targets_after"
cmp -s "$production_targets_before" "$production_targets_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

curl --fail --silent --show-error --max-time 30 -X DELETE \
  "$QDRANT_URL/collections/$collection" >/dev/null
collection_exists=0
[[ "$(curl --silent --show-error --max-time 30 -o /dev/null -w '%{http_code}' \
  "$QDRANT_URL/collections/$collection")" == 404 ]]
docker exec "$container" dropdb -U sage --if-exists --force "$clone_db"
clone_exists=0
[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$source_db" -c \
  "SELECT count(*) FROM pg_database WHERE datname='$clone_db'" | tr -d '[:space:]')" == 0 ]]

report="$artifact_dir/report.json"
jq -n \
  --arg head "$head" \
  --arg plan_sha256 "$expected_plan_sha" \
  --arg plan_file_sha256 "$(sha256sum "$plan" | awk '{print $1}')" \
  --arg runner_result_sha256 "$(sha256sum "$result" | awk '{print $1}')" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:"memory_v1_v5_2_reviewed_claim_projection_clone_report_v1",
    head_commit:$head,
    plan_sha256:$plan_sha256,
    plan_file_sha256:$plan_file_sha256,
    runner_result_sha256:$runner_result_sha256,
    exact_claims:19,
    new_projections:19,
    revision_replacements:0,
    deterministic_local_embedding_calls:19,
    external_model_calls:0,
    cross_owner_claim_count:0,
    cross_owner_outbox_count:0,
    cross_owner_insert_rejected:true,
    clone_non_outbox_memory_unchanged:true,
    production_target_claims_and_outbox_unchanged:true,
    production_qdrant_sha256:$qdrant_sha256,
    production_qdrant_unchanged:true,
    replay_rows_written:0,
    clone_deleted:true,
    temporary_collection_deleted:true,
    retrieval_activation:false,
    prompt_influence:false
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
printf 'plan=%s\n' "$plan"
printf 'plan_sha256=%s\n' "$expected_plan_sha"
printf 'report=%s\n' "$report"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'memory_v1_v5_2_reviewed_claim_projection_clone: PASS\n'
