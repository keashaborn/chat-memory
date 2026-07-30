#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Reviews the exact staged V5.2 claim targets in a
# disposable production clone. It cannot materialize claims or write Qdrant.

if [[ "$#" -ne 6 ]]; then
  echo 'usage: clone.sh STAGE_MANIFEST REVIEW_MANIFEST AUTH CROSS APPLY REPLAY' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
stage_manifest=$(realpath "$1")
review_manifest=$(realpath -m "$2")
authorization=$(realpath -m "$3")
cross_result=$(realpath -m "$4")
apply_result=$(realpath -m "$5")
replay_result=$(realpath -m "$6")
review_root=/home/ubuntu/memory-v1-reviews
runner=scripts/memory_v1_v5_2_claim_target_review_batch.py
python_bin=/opt/chat-memory/venv/bin/python
container=brains-postgres-1
source_db=memory
clone_db="memory_claim_target_review_$(date -u +%Y%m%d%H%M%S)_$$"
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
confirmation=REVIEW_EXACT_STAGED_V5_2_CLAIM_TARGETS_ONLY

[[ -x "$python_bin" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
stage_head=$(jq -er '.required_head_commit' "$stage_manifest")
git -C "$repo_root" merge-base --is-ancestor "$stage_head" "$head"
[[ "$(jq -er '.contract_version' "$stage_manifest")" == \
   memory_v1_v5_2_claim_target_stage_manifest_v1 ]]
[[ "$(jq -er '.stage_item_count' "$stage_manifest")" == 2 ]]
for path in \
  "$stage_manifest" "$review_manifest" "$authorization" "$cross_result" \
  "$apply_result" "$replay_result"; do
  [[ "$path" == "$review_root"/* ]]
done
[[ -f "$stage_manifest" && "$(stat -c '%a' "$stage_manifest")" == 600 ]]
for path in \
  "$review_manifest" "$authorization" "$cross_result" "$apply_result" \
  "$replay_result"; do
  [[ ! -e "$path" ]]
done

env_file="$repo_root/.env"
[[ -r "$env_file" ]] || env_file=/opt/chat-memory/.env
[[ -r "$env_file" ]]
set -a
source "$env_file"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]

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
    | sha256sum | awk '{print $1}'
}

clone_counts() {
  docker exec "$container" psql -X -A -t -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT jsonb_build_object(
        'claim',(SELECT count(*) FROM memory.claim),
        'claim_revision',(SELECT count(*) FROM memory.claim_revision),
        'claim_observation',(SELECT count(*) FROM memory.claim_observation),
        'entity',(SELECT count(*) FROM memory.entity),
        'observation',(SELECT count(*) FROM memory.observation),
        'projection_plan',(SELECT count(*) FROM memory.projection_plan),
        'projection_plan_item',(SELECT count(*) FROM memory.projection_plan_item),
        'projection_plan_observation',
          (SELECT count(*) FROM memory.projection_plan_observation),
        'projection_review',(SELECT count(*) FROM memory.projection_review),
        'projection_apply_event',
          (SELECT count(*) FROM memory.projection_apply_event),
        'projection_outbox',(SELECT count(*) FROM memory.projection_outbox)
      )::text"
}

production_signature() {
  docker exec "$container" psql -X -A -t -U sage -d "$source_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT count(*)::text || ':' ||
             encode(public.digest(convert_to(coalesce(string_agg(row_value,E'\\n'
             ORDER BY row_value),''),'UTF8'),'sha256'),'hex')
        FROM (
          SELECT to_jsonb(value)::text AS row_value
            FROM memory.projection_review AS value
        ) AS rows"
}

production_before=$(production_signature)
qdrant_before=$(qdrant_signature)
docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" python3 - <<'PY'
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

PYTHONPATH="$repo_root" POSTGRES_DSN="$clone_dsn" \
  "$python_bin" "$repo_root/$runner" manifest \
  --stage-manifest "$stage_manifest" \
  --required-head "$head" \
  --output "$review_manifest"
[[ "$(jq -er '.expected_new_rows' "$review_manifest")" == 2 ]]
[[ "$(jq -er '[.items[].target_action]|sort|join(",")' \
      "$review_manifest")" == create,reinforce ]]

PYTHONPATH="$repo_root" "$python_bin" "$repo_root/$runner" authorize \
  --manifest "$review_manifest" \
  --output "$authorization"

PYTHONPATH="$repo_root" POSTGRES_DSN="$clone_dsn" \
  "$python_bin" "$repo_root/$runner" cross-owner \
  --manifest "$review_manifest" \
  --other-owner "$other_owner" \
  --output "$cross_result"
[[ "$(jq -er '.cross_owner_rejected' "$cross_result")" == true ]]
[[ "$(jq -er '.rows_written' "$cross_result")" == 0 ]]

before=$(clone_counts)
PYTHONPATH="$repo_root" POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CLAIM_TARGET_REVIEW_APPLY=authorized \
  "$python_bin" "$repo_root/$runner" apply \
  --manifest "$review_manifest" \
  --authorization "$authorization" \
  --confirm "$confirmation" \
  --output "$apply_result"
[[ "$(jq -er '.rows_written' "$apply_result")" == 2 ]]
[[ "$(jq -er '[.outcomes[].target_action]|sort|join(",")' \
      "$apply_result")" == create,reinforce ]]

after=$(clone_counts)
BEFORE="$before" AFTER="$after" python3 - <<'PY'
import json
import os
before=json.loads(os.environ["BEFORE"])
after=json.loads(os.environ["AFTER"])
for table,value in before.items():
    wanted=2 if table=="projection_review" else 0
    actual=after[table]-value
    if actual != wanted:
        raise SystemExit(f"unexpected clone delta {table}: {actual} != {wanted}")
PY

PYTHONPATH="$repo_root" POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CLAIM_TARGET_REVIEW_APPLY=authorized \
  "$python_bin" "$repo_root/$runner" replay \
  --manifest "$review_manifest" \
  --authorization "$authorization" \
  --confirm "$confirmation" \
  --output "$replay_result"
[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]
[[ "$(clone_counts)" == "$after" ]]
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf 'review_rows_created=2\n'
printf 'claim_rows_created=0\n'
printf 'qdrant_writes=0\n'
printf 'memory_v1_v5_2_claim_target_review_clone: PASS\n'
