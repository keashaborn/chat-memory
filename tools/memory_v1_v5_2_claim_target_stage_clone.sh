#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Revalidates and stages the three exact claim targets on a
# disposable production clone. It never creates claims or touches Qdrant,
# retrieval, answer bindings, or prompts.

repo_root=$(git rev-parse --show-toplevel)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_claim_target_review_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
dahlia=917ab793-6f03-4af4-847b-c87f5632fa91
helsing=14e21c6b-1728-439b-9613-7d9b933d33b8
keasha=c0194481-bed5-438f-9407-07e398f14e50
python_bin=/opt/chat-memory/venv/bin/python
review_dir=/home/ubuntu/memory-v1-reviews
run_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
review="$review_dir/claim-target-stage-review-$run_id.json"
apply_result="$review_dir/claim-target-stage-apply-$run_id.json"
replay_result="$review_dir/claim-target-stage-replay-$run_id.json"

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ "$(stat -c '%a' "$review_dir")" == 700 ]]

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
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
print(urlunsplit((
    source.scheme,
    source.netloc,
    "/" + os.environ["CLONE_DB"],
    source.query,
    source.fragment,
)))
PY
)

count() {
  docker exec "$container" psql -X -A -t -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM memory.$1;"
}
before_claim=$(count claim)
before_revision=$(count claim_revision)
before_link=$(count claim_observation)
before_entity=$(count entity)
before_observation=$(count observation)
before_plan=$(count projection_plan)
before_item=$(count projection_plan_item)
before_payload=$(count projection_claim_payload)
before_plan_link=$(count projection_plan_observation)
before_review=$(count projection_review)
before_apply=$(count projection_apply_event)
before_outbox=$(count projection_outbox)

common_env=(
  POSTGRES_DSN="$clone_dsn"
  MEMORY_V1_DISPOSABLE_CLONE_REQUIRED=1
  PYTHONPATH="$repo_root:$repo_root/scripts"
)
env "${common_env[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review.py" \
  --owner "$owner" \
  --observation "$dahlia" \
  --observation "$helsing" \
  --observation "$keasha" \
  --output "$review"

env "${common_env[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" \
  --review "$review" --output "$apply_result"
[[ "$(jq -er '.rows_written' "$apply_result")" == 0 ]]
[[ "$(jq -er '.outcome_counts.missing_entailment' "$apply_result")" == 2 ]]
[[ "$(jq -er '.outcome_counts.manual_review' "$apply_result")" == 1 ]]

[[ "$(count claim)" == "$before_claim" ]]
[[ "$(count claim_revision)" == "$before_revision" ]]
[[ "$(count claim_observation)" == "$before_link" ]]
[[ "$(count entity)" == "$before_entity" ]]
[[ "$(count observation)" == "$before_observation" ]]
[[ "$(count projection_plan)" == "$before_plan" ]]
[[ "$(count projection_plan_item)" == "$before_item" ]]
[[ "$(count projection_claim_payload)" == "$before_payload" ]]
[[ "$(count projection_plan_observation)" == "$before_plan_link" ]]
[[ "$(count projection_review)" == "$before_review" ]]
[[ "$(count projection_apply_event)" == "$before_apply" ]]
[[ "$(count projection_outbox)" == "$before_outbox" ]]

env "${common_env[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" \
  --review "$review" --output "$replay_result"
[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]
[[ "$(jq -er '.outcome_counts.missing_entailment' "$replay_result")" == 2 ]]
[[ "$(jq -er '.outcome_counts.manual_review' "$replay_result")" == 1 ]]
[[ "$(count projection_plan)" == "$before_plan" ]]
[[ "$(count projection_plan_item)" == "$before_item" ]]
[[ "$(count projection_claim_payload)" == "$before_payload" ]]
[[ "$(count projection_plan_observation)" == "$before_plan_link" ]]
[[ "$(count claim)" == "$before_claim" ]]
[[ "$(count projection_review)" == "$before_review" ]]
[[ "$(count projection_apply_event)" == "$before_apply" ]]
[[ "$(count projection_outbox)" == "$before_outbox" ]]

printf 'apply_result=%s\n' "$apply_result"
printf 'replay_result=%s\n' "$replay_result"
printf 'plans_staged=0\n'
printf 'missing_entailment_blocked=2\n'
printf 'renderer_drift_held=1\n'
printf 'claim_qdrant_retrieval_prompt_deltas=0\n'
printf 'memory_v1_v5_2_claim_target_stage_clone: PASS\n'
