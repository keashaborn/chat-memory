#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Clone-tests copying the eleven validated pet vectors from
# the isolated shadow collection into a temporary activation collection while
# updating only a disposable Postgres clone. No external call or live write.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
plan="$repo_root/evals/memory_v1_v5_2_pet_claim_projection_plan_20260729.json"
runner="$repo_root/scripts/memory_v1_v5_2_pet_claim_projection_activate.py"
expected_plan_sha=115e06d6c78ea3187f7e0e593fe1e8a697ed50a4981b36de21f8c0da404ef7b7
source_collection=memory_claim_v1_shadow_pet_115e06d6c78e
target_collection="memory_claim_v1_activation_clone_$(date -u +%Y%m%d%H%M%S)_$$"
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_pet_activation_$(date -u +%Y%m%d%H%M%S)_$$"
python_bin=/opt/chat-memory/venv/bin/python
artifact_dir="/home/ubuntu/memory-v1-reviews/pet-claim-projection-activation-clone-$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$repo_root" rev-parse --short=12 HEAD)"
table_list=$(mktemp /tmp/memory-v1-pet-activation-tables.XXXXXX)
clone_exists=0
collection_exists=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(sha256sum "$plan" | awk '{print $1}')" == "$expected_plan_sha" ]]
git -C "$repo_root" merge-base --is-ancestor \
  "$(jq -er '.required_ancestor_commit' "$plan")" HEAD
mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${QDRANT_URL:-}" ]]

cleanup() {
  if [[ "$collection_exists" -eq 1 ]]; then
    curl --silent --show-error --max-time 30 -X DELETE \
      "$QDRANT_URL/collections/$target_collection" >/dev/null 2>&1 || true
  fi
  if [[ "$clone_exists" -eq 1 ]]; then
    docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
      >/dev/null 2>&1 || true
  fi
  rm -f "$table_list"
}
trap cleanup EXIT

collection_signature() {
  local collection=$1
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    "$QDRANT_URL/collections/$collection/points/scroll" \
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

docker exec "$container" psql -X -A -t -U sage -d "$source_db" -c "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name <> 'projection_outbox'
  ORDER BY table_name
" >"$table_list"
[[ -s "$table_list" ]]
live_before=$(collection_signature memory_claim_v1)
shadow_before=$(collection_signature "$source_collection")

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
clone_memory_before="$artifact_dir/clone-memory-before.tsv"
clone_memory_after="$artifact_dir/clone-memory-after.tsv"
capture_memory_without_outbox "$clone_db" "$clone_memory_before"

collection_exists=1
TARGET_COLLECTION="$target_collection" SOURCE_COLLECTION="$source_collection" \
QDRANT_URL="$QDRANT_URL" PYTHONPATH="$repo_root" "$python_bin" - <<'PY'
import os
from qdrant_client.http import models as qmodels
from rag_engine.memory_v1_projection import ClaimVectorIndex
from rag_engine.qdrant_compat import make_qdrant_client

client = make_qdrant_client(url=os.environ["QDRANT_URL"], timeout=20.0)
try:
    target = ClaimVectorIndex(
        client, collection_name=os.environ["TARGET_COLLECTION"], vector_size=3072
    )
    target.ensure_collection()
    points = client.retrieve(
        collection_name="memory_claim_v1",
        ids=["bd20dd0a-9fa0-4a21-8a93-e828c8044150"],
        with_payload=True,
        with_vectors=True,
    )
    if len(points) != 1 or (points[0].payload or {}).get("revision_number") != 2:
        raise RuntimeError("live Neko revision-2 seed is absent")
    client.upsert(
        collection_name=os.environ["TARGET_COLLECTION"],
        wait=True,
        points=[
            qmodels.PointStruct(
                id=str(points[0].id),
                vector=points[0].vector,
                payload=points[0].payload or {},
            )
        ],
    )
finally:
    client.close()
PY

apply="$artifact_dir/activation-apply.json"
replay="$artifact_dir/activation-replay.json"
POSTGRES_DSN="$clone_dsn" QDRANT_URL="$QDRANT_URL" \
MEMORY_V1_V5_2_PET_CLAIM_ACTIVATION=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$runner" --mode apply --plan "$plan" \
  --source-collection "$source_collection" \
  --target-collection "$target_collection" --output "$apply"
[[ "$(jq -er '.mode' "$apply")" == apply ]]
[[ "$(jq -er '.claim_count' "$apply")" == 11 ]]
[[ "$(jq -er '.embedding_requests' "$apply")" == 0 ]]
[[ "$(jq -er '.external_model_calls' "$apply")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$apply")" == 11 ]]
[[ "$(jq -er '[.shadow_tests[]|select(
  .selected_count>=1 and .other_owner_database_record_count==0 and
  .prompt_influence==false)]|length' "$apply")" == 11 ]]

POSTGRES_DSN="$clone_dsn" QDRANT_URL="$QDRANT_URL" \
MEMORY_V1_V5_2_PET_CLAIM_ACTIVATION_REPLAY=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$runner" --mode replay --plan "$plan" \
  --source-collection "$source_collection" \
  --target-collection "$target_collection" --output "$replay"
[[ "$(jq -er '.mode' "$replay")" == replay ]]
[[ "$(jq -er '.embedding_requests' "$replay")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$replay")" == 0 ]]
[[ "$(jq -er '.postgres_rows_mutated' "$replay")" == 0 ]]

capture_memory_without_outbox "$clone_db" "$clone_memory_after"
cmp -s "$clone_memory_before" "$clone_memory_after"
[[ "$(collection_signature memory_claim_v1)" == "$live_before" ]]
[[ "$(collection_signature "$source_collection")" == "$shadow_before" ]]

curl --fail --silent --show-error --max-time 30 -X DELETE \
  "$QDRANT_URL/collections/$target_collection" >/dev/null
collection_exists=0
docker exec "$container" dropdb -U sage --if-exists --force "$clone_db"
clone_exists=0

report="$artifact_dir/report.json"
jq -n \
  --arg head "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg plan_sha256 "$expected_plan_sha" \
  --arg apply_sha256 "$(sha256sum "$apply" | awk '{print $1}')" \
  --arg replay_sha256 "$(sha256sum "$replay" | awk '{print $1}')" \
  --arg live_qdrant_sha256 "$live_before" \
  --arg shadow_qdrant_sha256 "$shadow_before" \
  '{
    contract_version:"memory_v1_v5_2_pet_claim_projection_activation_clone_report_v1",
    head_commit:$head,
    plan_sha256:$plan_sha256,
    apply_file_sha256:$apply_sha256,
    replay_file_sha256:$replay_sha256,
    exact_claims:11,
    external_model_calls:0,
    clone_postgres_outbox_rows_applied:11,
    temporary_qdrant_points_applied:11,
    owner_shadow_tests_passed:11,
    cross_owner_isolation:true,
    clone_non_outbox_memory_unchanged:true,
    live_qdrant_sha256:$live_qdrant_sha256,
    live_qdrant_unchanged:true,
    shadow_qdrant_sha256:$shadow_qdrant_sha256,
    shadow_qdrant_unchanged:true,
    replay_writes:0,
    clone_deleted:true,
    temporary_collection_deleted:true,
    prompt_configuration_changes:0
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
printf 'report=%s\n' "$report"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'memory_v1_v5_2_pet_claim_projection_activation_clone: PASS\n'
