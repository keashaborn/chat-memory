#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Reviews exact claim targets against a disposable
# production clone. It does not stage plans or mutate claims, Qdrant, retrieval,
# answer bindings, or prompts.

repo_root=$(git rev-parse --show-toplevel)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_claim_target_review_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
dahlia=917ab793-6f03-4af4-847b-c87f5632fa91
helsing=14e21c6b-1728-439b-9613-7d9b933d33b8
keasha=c0194481-bed5-438f-9407-07e398f14e50
python_bin=/opt/chat-memory/venv/bin/python
review_dir=/home/ubuntu/memory-v1-reviews
run_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
result="$review_dir/claim-target-review-$run_id.json"
foreign_result="$review_dir/claim-target-review-foreign-$run_id.json"

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
[[ ! -e "$result" && ! -e "$foreign_result" ]]

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
  rm -f "$foreign_result"
}
trap cleanup EXIT

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

tables=(
  entity observation observation_entity_binding
  claim claim_revision claim_observation
  projection_plan projection_plan_item projection_claim_payload
  projection_review projection_apply_event projection_outbox
)
snapshot_counts() {
  local database=$1
  for table in "${tables[@]}"; do
    docker exec "$container" psql -X -A -t -U sage -d "$database" \
      -v ON_ERROR_STOP=1 -c \
      "SELECT '$table='||count(*) FROM memory.$table;"
  done
}
before=$(snapshot_counts "$clone_db")

POSTGRES_DSN="$clone_dsn" MEMORY_V1_DISPOSABLE_CLONE_REQUIRED=1 \
PYTHONPATH="$repo_root:$repo_root/scripts" \
"$python_bin" "$repo_root/scripts/memory_v1_v5_2_claim_target_review.py" \
  --owner "$owner" \
  --observation "$dahlia" \
  --observation "$helsing" \
  --observation "$keasha" \
  --output "$result"

[[ "$(jq -er '.item_count' "$result")" == 3 ]]
[[ "$(jq -er '.action_counts.create' "$result")" == 1 ]]
[[ "$(jq -er '.action_counts.manual_review' "$result")" == 1 ]]
[[ "$(jq -er '.action_counts.reinforce' "$result")" == 1 ]]
[[ "$(jq -er --arg id "$keasha" \
  '.items[]|select(.observation_id==$id)|.action' "$result")" == reinforce ]]
[[ "$(jq -er --arg id "$dahlia" \
  '.items[]|select(.observation_id==$id)|.action' "$result")" == manual_review ]]
[[ "$(jq -cer --arg id "$dahlia" \
  '.items[]|select(.observation_id==$id)|.reason_codes' "$result")" \
  == '["existing_semantic_aggregate_render_drift"]' ]]
[[ "$(jq -er --arg id "$helsing" \
  '.items[]|select(.observation_id==$id)|.action' "$result")" == create ]]
[[ "$(jq -er --arg id "$helsing" \
  '.items[]|select(.observation_id==$id)|.canonical_text' "$result")" \
  == 'Helsing died.' ]]
[[ "$(jq -er '.proofs' "$result")" == *'"database_writes": 0'* ]]
[[ "$(jq -er '.proofs.disposable_clone_verified' "$result")" == true ]]

after=$(snapshot_counts "$clone_db")
[[ "$after" == "$before" ]]

set +e
POSTGRES_DSN="$clone_dsn" MEMORY_V1_DISPOSABLE_CLONE_REQUIRED=1 \
PYTHONPATH="$repo_root:$repo_root/scripts" \
"$python_bin" "$repo_root/scripts/memory_v1_v5_2_claim_target_review.py" \
  --owner "$other_owner" \
  --observation "$keasha" \
  --output "$foreign_result" >/dev/null 2>&1
foreign_status=$?
set -e
[[ "$foreign_status" -ne 0 && ! -e "$foreign_result" ]]
[[ "$(snapshot_counts "$clone_db")" == "$before" ]]

printf 'result=%s\n' "$result"
printf 'actions=create:1,reinforce:1,manual_review:1\n'
printf 'cross_owner_rejected=true\n'
printf 'protected_row_deltas=0\n'
printf 'memory_v1_v5_2_claim_target_review_clone: PASS\n'
