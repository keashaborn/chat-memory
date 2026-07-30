#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Seeds the exact shadow collection with deterministic
# local vectors from a disposable database, runs the activation-clone suite,
# then removes every temporary resource. No external model call or live write.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
plan=/home/ubuntu/memory-v1-reviews/reviewed-claim-projection-clone-20260730T172334Z-950aa2b01dbd/projection-plan.json
seed_runner="$repo_root/scripts/memory_v1_v5_2_reviewed_claim_projection_clone.py"
activation_runner="$repo_root/tools/memory_v1_v5_2_reviewed_claim_projection_activation_clone.sh"
required_ancestor=950aa2b01dbd9a25a8d7662483ce3b6ca1e5c25e
source_collection=memory_claim_v1_shadow_reviewed_1ae1c4448c5c
container=brains-postgres-1
source_db=memory
seed_db="memory_v5_reviewed_activation_seed_$(date -u +%Y%m%d%H%M%S)_$$"
python_bin=/opt/chat-memory/venv/bin/python
artifact_dir="/home/ubuntu/memory-v1-reviews/reviewed-claim-projection-activation-synthetic-$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$repo_root" rev-parse --short=12 HEAD)"
seed_db_exists=0
source_exists=0

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
install -d -m 0700 "$artifact_dir"
set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${QDRANT_URL:-}" ]]
[[ "$(curl --silent --show-error --max-time 30 -o /dev/null -w '%{http_code}' \
  "$QDRANT_URL/collections/$source_collection")" == 404 ]]

cleanup() {
  if [[ "$source_exists" -eq 1 ]]; then
    curl --silent --show-error --max-time 30 -X DELETE \
      "$QDRANT_URL/collections/$source_collection" >/dev/null 2>&1 || true
  fi
  if [[ "$seed_db_exists" -eq 1 ]]; then
    docker exec "$container" dropdb -U sage --if-exists --force "$seed_db" \
      >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

docker exec "$container" createdb -U sage -T template0 "$seed_db"
seed_db_exists=1
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$seed_db"
seed_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$seed_db" "$python_bin" - <<'PY'
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

source_exists=1
seed_result="$artifact_dir/seed-result.json"
MEMORY_V1_REQUIRED_ANCESTOR="$required_ancestor" POSTGRES_DSN="$seed_dsn" \
QDRANT_URL="$QDRANT_URL" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$seed_runner" --plan "$plan" --output "$seed_result" \
  --collection "$source_collection"
[[ "$(jq -er '.claim_count' "$seed_result")" == 19 ]]
[[ "$(jq -er '.embedding_requests' "$seed_result")" == 19 ]]
[[ "$(jq -er '.external_model_calls' "$seed_result")" == 0 ]]
[[ "$(jq -er '.projection_result.upserted' "$seed_result")" == 19 ]]
[[ "$(jq -er '.replay_result.claimed' "$seed_result")" == 0 ]]

docker exec "$container" dropdb -U sage --if-exists --force "$seed_db"
seed_db_exists=0

"$activation_runner" | tee "$artifact_dir/activation-clone.stdout"
chmod 0600 "$artifact_dir/activation-clone.stdout"

curl --fail --silent --show-error --max-time 30 -X DELETE \
  "$QDRANT_URL/collections/$source_collection" >/dev/null
source_exists=0
[[ "$(curl --silent --show-error --max-time 30 -o /dev/null -w '%{http_code}' \
  "$QDRANT_URL/collections/$source_collection")" == 404 ]]

report="$artifact_dir/report.json"
jq -n \
  --arg head "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg seed_result_sha256 "$(sha256sum "$seed_result" | awk '{print $1}')" \
  '{
    contract_version:"memory_v1_v5_2_reviewed_claim_projection_activation_synthetic_clone_report_v1",
    head_commit:$head,
    deterministic_seed_claims:19,
    deterministic_seed_result_sha256:$seed_result_sha256,
    external_model_calls:0,
    activation_clone_passed:true,
    seed_database_deleted:true,
    source_collection_deleted:true,
    production_postgres_writes:0,
    production_qdrant_writes:0,
    prompt_influence:false
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
printf 'report=%s\n' "$report"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'memory_v1_v5_2_reviewed_claim_projection_activation_synthetic_clone: PASS\n'
