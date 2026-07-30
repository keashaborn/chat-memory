#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Stages reviewed generic create/reinforce targets in a
# disposable production clone. It never creates claims or touches Qdrant.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_claim_target_review_stage_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
dahlia=917ab793-6f03-4af4-847b-c87f5632fa91
helsing=14e21c6b-1728-439b-9613-7d9b933d33b8
keasha=c0194481-bed5-438f-9407-07e398f14e50
python_bin=/opt/chat-memory/venv/bin/python
review_dir=/home/ubuntu/memory-v1-reviews
head_commit=$(git rev-parse HEAD)
run_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
review="$review_dir/claim-target-stage-review-$run_id.json"
manifest="$review_dir/claim-target-stage-manifest-$run_id.json"
authorization="$review_dir/claim-target-stage-authorization-$run_id.json"
cross_owner="$review_dir/claim-target-stage-cross-owner-$run_id.json"
apply_result="$review_dir/claim-target-stage-apply-$run_id.json"
replay_result="$review_dir/claim-target-stage-replay-$run_id.json"

if [[ -r "$repo_root/.env" ]]; then
  set -a
  source "$repo_root/.env"
  set +a
elif [[ -r /opt/chat-memory/.env ]]; then
  set -a
  source /opt/chat-memory/.env
  set +a
fi
[[ -n "${POSTGRES_DSN:-}" ]]
[[ "$(stat -c '%a' "$review_dir")" == 700 ]]

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | cut -d' ' -f1
}

tables=(
  entity observation observation_entity_binding observation_entailment_v5
  claim claim_revision claim_observation
  projection_plan projection_plan_item projection_claim_payload
  projection_plan_observation projection_review projection_apply_event
  projection_outbox
)
snapshot_counts() {
  local database=$1
  for table in "${tables[@]}"; do
    docker exec "$container" psql -X -A -t -U sage -d "$database" \
      -v ON_ERROR_STOP=1 -c \
      "SELECT '$table='||count(*) FROM memory.$table;"
  done
}

production_target_signature() {
  docker exec "$container" psql -X -A -t -U sage -d "$source_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT jsonb_build_object(
        'claims',(
          SELECT COALESCE(jsonb_agg(to_jsonb(value) ORDER BY value.claim_id),'[]')
          FROM memory.claim AS value
          WHERE value.claim_id IN (
            '7c1813ff-7569-4713-bbdf-108ba4312e40',
            '8153ff74-0357-48e6-b341-39be1a54353d'
          )
             OR value.canonical_key IN (
               'v5:e4c548806fddd708ae9c20e659e660265cf6f5d08a71f9ef49e6f06eb1bade10'
             )
        ),
        'plans',(
          SELECT COALESCE(jsonb_agg(to_jsonb(value) ORDER BY value.plan_id),'[]')
          FROM memory.projection_plan AS value
          WHERE value.plan_id IN (
            'de2eae0e-a888-5d6a-8b9f-ae77014f10f4',
            '9bdfcdab-0a38-59d8-9dc9-2cb9e359fd75',
            '834783c0-6c1d-577b-8f85-385ef38cbe99'
          )
        )
      )::text;" | sha256sum | cut -d' ' -f1
}

production_before=$(production_target_signature)
qdrant_before=$(qdrant_signature)

docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"
docker exec "$container" psql -X -U sage -d postgres -v ON_ERROR_STOP=1 \
  -c "COMMENT ON DATABASE \"$clone_db\" IS
      'memory_v1_v5_2_claim_target_review_clone_v1';" >/dev/null

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

source = urlsplit(os.environ["SOURCE_DSN"])
if source.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("unsupported PostgreSQL DSN scheme")
print(urlunsplit((
    source.scheme,
    source.netloc,
    "/" + os.environ["CLONE_DB"],
    source.query,
    source.fragment,
)))
PY
)

clone_before=$(snapshot_counts "$clone_db")
runtime=(
  env
  POSTGRES_DSN="$clone_dsn"
  PYTHONPATH="$repo_root:$repo_root/scripts"
)

"${runtime[@]}" MEMORY_V1_DISPOSABLE_CLONE_REQUIRED=1 \
  "$python_bin" "$repo_root/scripts/memory_v1_v5_2_claim_target_review.py" \
  --owner "$owner" \
  --observation "$dahlia" \
  --observation "$helsing" \
  --observation "$keasha" \
  --output "$review"

"${runtime[@]}" \
  "$python_bin" "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" \
  manifest \
  --owner "$owner" \
  --review-report "$review" \
  --required-head "$head_commit" \
  --output "$manifest"

[[ "$(jq -er '.stage_item_count' "$manifest")" == 2 ]]
[[ "$(jq -er '.held_item_count' "$manifest")" == 1 ]]
[[ "$(jq -er '.expected_rows' "$manifest")" == 8 ]]
[[ "$(jq -cer '.stage_items|map(.action)|sort' "$manifest")" \
  == '["create","reinforce"]' ]]
[[ "$(jq -cer '.held_items|map(.reason_codes)|add' "$manifest")" \
  == '["existing_semantic_aggregate_render_drift"]' ]]

"${runtime[@]}" \
  "$python_bin" "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" \
  authorize \
  --manifest "$manifest" \
  --output "$authorization"

"${runtime[@]}" \
  "$python_bin" "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" \
  cross-owner \
  --manifest "$manifest" \
  --other-owner "$other_owner" \
  --output "$cross_owner"
[[ "$(jq -er '.cross_owner_rejected' "$cross_owner")" == true ]]

"${runtime[@]}" \
  MEMORY_V1_REQUIRED_HEAD="$head_commit" \
  MEMORY_V1_V5_2_CLAIM_TARGET_STAGE_APPLY=authorized \
  "$python_bin" "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" \
  apply \
  --manifest "$manifest" \
  --authorization "$authorization" \
  --confirm STAGE_REVIEWED_V5_2_CLAIM_TARGETS_ONLY \
  --output "$apply_result"

[[ "$(jq -er '.rows_written' "$apply_result")" == 8 ]]
[[ "$(jq -er '.claims_written' "$apply_result")" == 0 ]]
[[ "$(jq -cer '.outcomes|map(.action)|sort' "$apply_result")" \
  == '["create","reinforce"]' ]]

"${runtime[@]}" \
  MEMORY_V1_REQUIRED_HEAD="$head_commit" \
  MEMORY_V1_V5_2_CLAIM_TARGET_STAGE_APPLY=authorized \
  "$python_bin" "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" \
  replay \
  --manifest "$manifest" \
  --authorization "$authorization" \
  --confirm STAGE_REVIEWED_V5_2_CLAIM_TARGETS_ONLY \
  --output "$replay_result"

[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]
[[ "$(jq -cer '.outcomes|map(.outcome)|unique' "$replay_result")" \
  == '["replayed"]' ]]

delta() {
  local table=$1
  local before after
  before=$(printf '%s\n' "$clone_before" | sed -n "s/^$table=//p")
  after=$(snapshot_counts "$clone_db" | sed -n "s/^$table=//p")
  printf '%s' "$((after - before))"
}

[[ "$(delta projection_plan)" == 2 ]]
[[ "$(delta projection_plan_item)" == 2 ]]
[[ "$(delta projection_claim_payload)" == 2 ]]
[[ "$(delta projection_plan_observation)" == 2 ]]
for table in entity observation observation_entity_binding \
  observation_entailment_v5 claim claim_revision claim_observation \
  projection_review projection_apply_event projection_outbox; do
  [[ "$(delta "$table")" == 0 ]]
done

[[ "$(production_target_signature)" == "$production_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' \
  'CLAIM_TARGET_STAGE_CLONE=PASS' \
  "REVIEW=$review" \
  "MANIFEST=$manifest" \
  "APPLY_RESULT=$apply_result" \
  'STAGE_ITEMS=2' \
  'HELD_ITEMS=1' \
  'ROWS_WRITTEN=8' \
  'REPLAY_ROWS=0' \
  'CLAIM_WRITES=0' \
  'QDRANT_WRITES=0' \
  'PRODUCTION_WRITES=0'
