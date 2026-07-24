#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the additive reconciliation functions in a
# disposable production clone, then stages, reviews, materializes, and replays
# one exact owner-scoped reported stance. Production is never written.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -d "$repo_root/.git" || -f "$repo_root/.git" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_reconciled_stance_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
evidence=22bd0732-3539-4180-8f89-8f84114131c0
primary=807fa195-669a-4a7d-bb60-d8a50ea22bbb
context=560261e2-7ac6-435d-bcb6-934315b78472
migration=ops/sql/20260724_memory_v1_v5_2_reconciled_stance_projection.sql
python_bin=/opt/chat-memory/venv/bin/python
artifact_dir="/home/ubuntu/memory-v1-reviews/reconciled-clone-$(date -u +%Y%m%dT%H%M%SZ)-${head:0:12}"
stage_runner=scripts/memory_v1_v5_2_reconciled_stance_stage.py
review_manifest_runner=scripts/memory_v1_v5_2_reconciled_stance_review_manifest.py
review_runner=scripts/memory_v1_v5_2_projection_review_batch.py
apply_manifest_runner=scripts/memory_v1_v5_2_reconciled_stance_apply_manifest.py
apply_runner=scripts/memory_v1_v5_claim_projection_apply_batch.py

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ -x "$python_bin" ]]
[[ -f "$repo_root/$migration" ]]
mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

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
docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" <"$repo_root/$migration"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts" \
  "$python_bin" - "$owner" "$primary" <<'PY'
import asyncio
import json
import os
import sys
import uuid

import asyncpg
from memory_v1_v5_2_projection_dispatch import build_packet, stable_json


async def main() -> None:
    owner = str(uuid.UUID(sys.argv[1]))
    observation_id = uuid.UUID(sys.argv[2])
    connection = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    transaction = connection.transaction(readonly=True, isolation="serializable")
    await transaction.start()
    try:
        await connection.execute("SELECT set_config('app.user_id',$1,true)", owner)
        row = await connection.fetchrow(
            "SELECT * FROM memory.preflight_projection_source_v5_2($1)",
            observation_id,
        )
        source = dict(row)
        for field in ("object_literal", "project_scope", "temporal"):
            if isinstance(source.get(field), str):
                source[field] = json.loads(source[field])
        packet = build_packet(owner, source)
        if packet["projector_version"] != "semantic_dispatch_v2":
            raise RuntimeError("standard dispatcher did not emit v2")
        expected_text = (
            'The user reports this position: "I believe we can’t read the future '
            'so worrying about what’s gonna happen next month doesn’t really matter."'
        )
        if packet["projections"][0]["payload"]["canonical_text"] != expected_text:
            raise RuntimeError("standard stance renderer v2 mismatch")
        plan_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memory-v1-v5-2-standard-v2-clone|{owner}|{observation_id}",
        )
        preflight = await connection.fetchrow(
            "SELECT * FROM memory.preflight_projection_packet_v5_2($1,$2)",
            plan_id,
            stable_json(packet),
        )
        if (
            preflight["existing_aggregates"] != 0
            or preflight["existing_plans"] != 0
        ):
            raise RuntimeError("standard v2 preflight is not empty and stageable")
    finally:
        await transaction.rollback()
        await connection.close()


asyncio.run(main())
PY

qdrant_before=$(
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
)

table_list="$artifact_dir/memory-tables.tsv"
non_target_before="$artifact_dir/non-target-before.tsv"
non_target_after="$artifact_dir/non-target-after.tsv"
docker exec "$container" psql -X -A -F $'\t' -t -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" -c \
  "SELECT table_name,EXISTS(
     SELECT 1 FROM information_schema.columns AS c
     WHERE c.table_schema='memory'
       AND c.table_name=t.table_name
       AND c.column_name='owner_user_id'
   )
   FROM information_schema.tables AS t
   WHERE table_schema='memory' AND table_type='BASE TABLE'
   ORDER BY table_name" >"$table_list"

capture_non_target() {
  local output=$1 table has_owner predicate state
  : >"$output"
  while IFS=$'\t' read -r table has_owner; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    if [[ "$has_owner" == t ]]; then
      predicate="owner_user_id IS DISTINCT FROM '$owner'::uuid"
    else
      predicate=true
    fi
    state=$(docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
      -U sage -d "$clone_db" -c \
      "SELECT count(*)::text || E'\\t' ||
         encode(public.digest(convert_to(
           coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
           'UTF8'),'sha256'),'hex')
       FROM (
         SELECT to_jsonb(value)::text AS row_json
         FROM memory.\"$table\" AS value
         WHERE $predicate
       ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}
capture_non_target "$non_target_before"

stage_manifest="$artifact_dir/stage-manifest.json"
stage_authorization="$artifact_dir/stage-authorization.json"
stage_apply="$artifact_dir/stage-apply.json"
stage_replay="$artifact_dir/stage-replay.json"
isolation_result="$artifact_dir/isolation.json"
review_decisions="$artifact_dir/review-decisions.json"
review_manifest="$artifact_dir/review-manifest.json"
review_apply="$artifact_dir/review-apply.json"
review_replay="$artifact_dir/review-replay.json"
apply_manifest="$artifact_dir/apply-manifest.json"
materialize_apply="$artifact_dir/materialize-apply.json"
materialize_replay="$artifact_dir/materialize-replay.json"

env_common=(
  "POSTGRES_DSN=$clone_dsn"
  "PYTHONPATH=$repo_root/scripts"
  "MEMORY_V1_REQUIRED_HEAD=$head"
)
env "${env_common[@]}" "$python_bin" "$repo_root/$stage_runner" manifest \
  --owner "$owner" --evidence "$evidence" --primary "$primary" \
  --context "$context" --required-head "$head" --output "$stage_manifest"
env "${env_common[@]}" "$python_bin" "$repo_root/$stage_runner" authorize \
  --manifest "$stage_manifest" --output "$stage_authorization"
env "${env_common[@]}" "$python_bin" "$repo_root/$stage_runner" apply \
  --manifest "$stage_manifest" --authorization "$stage_authorization" \
  --confirm STAGE_ONE_OWNER_V5_2_RECONCILED_STANCE_ONLY \
  --output "$stage_apply"
env "${env_common[@]}" "$python_bin" "$repo_root/$stage_runner" replay \
  --manifest "$stage_manifest" --authorization "$stage_authorization" \
  --confirm STAGE_ONE_OWNER_V5_2_RECONCILED_STANCE_ONLY \
  --output "$stage_replay"
env "${env_common[@]}" "$python_bin" "$repo_root/$stage_runner" cross-owner \
  --manifest "$stage_manifest" --other-owner "$other_owner" \
  --output "$isolation_result"

env "${env_common[@]}" "$python_bin" "$repo_root/$review_manifest_runner" \
  --owner "$owner" --required-head "$head" \
  --stage-manifest "$stage_manifest" \
  --decisions-output "$review_decisions" --output "$review_manifest"
env "${env_common[@]}" MEMORY_V1_V5_2_PROJECTION_REVIEW_APPLY=authorized \
  "$python_bin" "$repo_root/$review_runner" --mode apply \
  --manifest "$review_manifest" --output "$review_apply"
env "${env_common[@]}" "$python_bin" "$repo_root/$review_runner" --mode replay \
  --manifest "$review_manifest" --output "$review_replay"

env "${env_common[@]}" "$python_bin" "$repo_root/$apply_manifest_runner" \
  --owner "$owner" --required-head "$head" \
  --stage-manifest "$stage_manifest" \
  --review-manifest "$review_manifest" --review-result "$review_apply" \
  --output "$apply_manifest"
env "${env_common[@]}" MEMORY_V1_CLAIM_PROJECTION_APPLY_BATCH=authorized \
  "$python_bin" "$repo_root/$apply_runner" --mode apply \
  --manifest "$apply_manifest" --output "$materialize_apply"
env "${env_common[@]}" "$python_bin" "$repo_root/$apply_runner" --mode replay \
  --manifest "$apply_manifest" --apply-result "$materialize_apply" \
  --output "$materialize_replay"

plan_id=$(jq -er '.plan_id' "$stage_manifest")
claim_id=$(jq -er '.outcomes[0].claim_id' "$materialize_apply")
canonical_text=$(
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone_db" -c \
    "SELECT canonical_text FROM memory.claim
     WHERE owner_user_id='$owner'::uuid AND claim_id='$claim_id'::uuid"
)
[[ "$canonical_text" == \
  'The user reports this position: "I believe we can’t read the future so worrying about what’s gonna happen next month doesn’t really matter."' ]]

verification=$(
  docker exec "$container" psql -X -A -F '|' -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone_db" -c \
    "SELECT
       (SELECT count(*) FROM memory.projection_plan
        WHERE owner_user_id='$owner'::uuid AND plan_id='$plan_id'::uuid),
       (SELECT count(*) FROM memory.projection_plan_item
        WHERE owner_user_id='$owner'::uuid AND plan_id='$plan_id'::uuid),
       (SELECT count(*) FROM memory.projection_claim_payload
        WHERE owner_user_id='$owner'::uuid AND plan_id='$plan_id'::uuid),
       (SELECT count(*) FROM memory.projection_plan_observation
        WHERE owner_user_id='$owner'::uuid AND plan_id='$plan_id'::uuid),
       (SELECT count(*) FROM memory.projection_review
        WHERE owner_user_id='$owner'::uuid AND plan_id='$plan_id'::uuid),
       (SELECT count(*) FROM memory.claim
        WHERE owner_user_id='$owner'::uuid AND claim_id='$claim_id'::uuid
          AND status='supported'),
       (SELECT count(*) FROM memory.claim_revision
        WHERE owner_user_id='$owner'::uuid AND claim_id='$claim_id'::uuid),
       (SELECT count(*) FROM memory.claim_observation
        WHERE owner_user_id='$owner'::uuid AND claim_id='$claim_id'::uuid),
       (SELECT count(*) FROM memory.projection_apply_event
        WHERE owner_user_id='$owner'::uuid AND plan_id='$plan_id'::uuid),
       (SELECT count(*) FROM memory.projection_dispatch_v5
        WHERE owner_user_id='$owner'::uuid
          AND resulting_claim_id='$claim_id'::uuid),
       (SELECT count(*) FROM memory.projection_outbox
        WHERE owner_user_id='$owner'::uuid AND aggregate_id='$claim_id'::uuid)"
)
[[ "$verification" == "1|1|1|2|1|1|2|2|1|1|0" ]]

stances=$(
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone_db" -c \
    "SELECT string_agg(
       link.observation_id::text || ':' || link.stance::text,
       ',' ORDER BY CASE link.stance WHEN 'supports' THEN 0 ELSE 1 END
     )
     FROM memory.projection_plan_observation AS link
     WHERE link.owner_user_id='$owner'::uuid
       AND link.plan_id='$plan_id'::uuid"
)
[[ "$stances" == \
  "$primary:supports,$context:context" ]]
[[ "$(jq -er '.rows_written' "$stage_apply")" == 5 ]]
[[ "$(jq -er '.rows_written' "$stage_replay")" == 0 ]]
[[ "$(jq -er '.rows_written' "$review_apply")" == 1 ]]
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]
[[ "$(jq -er '.insert_rows' "$materialize_apply")" == 12 ]]
[[ "$(jq -er '.mutated_rows' "$materialize_apply")" == 13 ]]
[[ "$(jq -er '.projection_outbox_deferred' "$materialize_apply")" == true ]]
[[ "$(jq -er '.insert_rows' "$materialize_replay")" == 0 ]]
[[ "$(jq -er '.mutated_rows' "$materialize_replay")" == 0 ]]
[[ "$(jq -er '.cross_owner_rejected' "$isolation_result")" == true ]]

capture_non_target "$non_target_after"
cmp -s "$non_target_before" "$non_target_after"
qdrant_after=$(
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
)
[[ "$qdrant_after" == "$qdrant_before" ]]

report="$artifact_dir/report.json"
jq -n \
  --arg head "$head" \
  --arg plan_id "$plan_id" \
  --arg claim_id "$claim_id" \
  --arg canonical_text "$canonical_text" \
  --arg qdrant_sha256 "$qdrant_after" \
  --arg stage_manifest_sha256 "$(jq -er '.manifest_sha256' "$stage_manifest")" \
  --arg apply_manifest_sha256 "$(jq -er '.manifest_sha256' "$apply_manifest")" \
  '{
    contract_version:"memory_v1_v5_2_reconciled_stance_clone_report_v1",
    head_commit:$head,
    plan_id:$plan_id,
    claim_id:$claim_id,
    canonical_text:$canonical_text,
    observation_links:{supports:1,context:1},
    stage_rows:5,
    review_rows:1,
    materialization_insert_rows:12,
    materialization_mutated_rows:13,
    projection_outbox_deferred:true,
    stage_replay_rows:0,
    review_replay_rows:0,
    materialization_replay_rows:0,
    cross_owner_rejected:true,
    non_target_unchanged:true,
    qdrant_sha256:$qdrant_sha256,
    stage_manifest_sha256:$stage_manifest_sha256,
    apply_manifest_sha256:$apply_manifest_sha256,
    production_writes:0,
    retrieval_activated:false,
    prompt_influence_activated:false
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
printf 'report=%s\n' "$report"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'memory_v1_v5_2_reconciled_stance_production_clone: PASS\n'
