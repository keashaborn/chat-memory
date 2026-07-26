#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Clone-tests the exact evidence-context stance projection
# admission. Production Postgres and Qdrant remain read-only; no model call,
# projection, retrieval activation, or prompt influence occurs.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
plan="$repo_root/evals/memory_v1_v5_2_evidence_context_stance_projection_plan.json"
expected_plan_sha=915605997c5449cc2cf4ebf2fa88c31968f09d06ee9d1befff23ac9bd813145e
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_compiler_v8_projection_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
runner=scripts/memory_v1_v5_deferred_projection_admission.py
python_bin=/opt/chat-memory/venv/bin/python
artifact_dir="/home/ubuntu/memory-v1-reviews/evidence-context-stance-projection-clone-$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$repo_root" rev-parse --short=12 HEAD)"

export GIT_OPTIONAL_LOCKS=0
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(sha256sum "$plan" | awk '{print $1}')" == "$expected_plan_sha" ]]
git -C "$repo_root" merge-base --is-ancestor \
  "$(jq -er '.required_ancestor_commit' "$plan")" HEAD
[[ "$(jq -er '.owner_user_id' "$plan")" == "$owner" ]]
[[ "$(jq -er '.items|length' "$plan")" == 1 ]]
apply_result=$(jq -er '.source.apply_result_path' "$plan")
apply_manifest=$(jq -er '.source.apply_manifest_path' "$plan")
[[ "$(sha256sum "$apply_result" | awk '{print $1}')" == \
  "$(jq -er '.source.apply_result_file_sha256' "$plan")" ]]
[[ "$(sha256sum "$apply_manifest" | awk '{print $1}')" == \
  "$(jq -er '.source.apply_manifest_file_sha256' "$plan")" ]]
required_head=$(jq -er '.required_head_commit' "$apply_manifest")
mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"

set -a
source /opt/chat-memory/.env
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

docker exec "$container" createdb -U sage -T template0 "$clone_db"
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

claim_csv=$(jq -r '[.items[].claim_id]|join(",")' "$plan")
[[ "$(docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" -c \
  "SELECT count(*) FROM memory.projection_outbox
   WHERE owner_user_id='$owner'::uuid
     AND aggregate_id=ANY(string_to_array('$claim_csv',',')::uuid[])")" == 0 ]]

qdrant_before=$(qdrant_signature)
preflight="$artifact_dir/preflight.json"
apply="$artifact_dir/apply.json"
replay="$artifact_dir/replay.json"
isolation="$artifact_dir/isolation.json"
env_common=(
  "POSTGRES_DSN=$clone_dsn"
  "PYTHONPATH=$repo_root/scripts"
  "MEMORY_V1_REQUIRED_HEAD=$required_head"
)
env "${env_common[@]}" "$python_bin" "$repo_root/$runner" \
  --mode preflight --apply-result "$apply_result" \
  --apply-manifest "$apply_manifest" --output "$preflight"
env "${env_common[@]}" MEMORY_V1_DEFERRED_PROJECTION_ADMISSION=authorized \
  "$python_bin" "$repo_root/$runner" \
  --mode apply --apply-result "$apply_result" \
  --apply-manifest "$apply_manifest" --output "$apply"
env "${env_common[@]}" "$python_bin" "$repo_root/$runner" \
  --mode replay --apply-result "$apply_result" \
  --apply-manifest "$apply_manifest" --prior-result "$apply" \
  --output "$replay"

POSTGRES_DSN="$clone_dsn" PLAN="$plan" OUTPUT="$isolation" \
OWNER="$owner" OTHER_OWNER="$other_owner" "$python_bin" - <<'PY'
import asyncio
import json
import os
from pathlib import Path
import uuid

import asyncpg


async def main() -> None:
    plan = json.loads(Path(os.environ["PLAN"]).read_text())
    claim_ids = [uuid.UUID(item["claim_id"]) for item in plan["items"]]
    owner = uuid.UUID(os.environ["OWNER"])
    other = os.environ["OTHER_OWNER"]
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        tx = conn.transaction(readonly=True, isolation="serializable")
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", other)
        visible_claims = await conn.fetchval(
            """SELECT count(*) FROM memory.claim
               WHERE owner_user_id=$1 AND claim_id=ANY($2::uuid[])""",
            owner,
            claim_ids,
        )
        visible_outbox = await conn.fetchval(
            """SELECT count(*) FROM memory.projection_outbox
               WHERE owner_user_id=$1 AND aggregate_id=ANY($2::uuid[])""",
            owner,
            claim_ids,
        )
        await tx.rollback()

        insert_rejected = False
        tx = conn.transaction(isolation="serializable")
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", other)
        try:
            await conn.execute(
                """INSERT INTO memory.projection_outbox(
                     owner_user_id,aggregate_type,aggregate_id,operation,payload
                   ) VALUES($1,'claim',$2,'upsert',$3::jsonb)""",
                owner,
                uuid.uuid4(),
                json.dumps(
                    {"claim_id": str(uuid.uuid4()), "revision_number": 2}
                ),
            )
        except (
            asyncpg.InsufficientPrivilegeError,
            asyncpg.CheckViolationError,
        ):
            insert_rejected = True
        finally:
            await tx.rollback()
        result = {
            "cross_owner_claim_count": visible_claims,
            "cross_owner_outbox_count": visible_outbox,
            "cross_owner_insert_rejected": insert_rejected,
        }
        if result != {
            "cross_owner_claim_count": 0,
            "cross_owner_outbox_count": 0,
            "cross_owner_insert_rejected": True,
        }:
            raise RuntimeError("cross-owner projection isolation failed")
        output = Path(os.environ["OUTPUT"])
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        output.chmod(0o600)
    finally:
        await conn.close()


asyncio.run(main())
PY

[[ "$(jq -er '.rows_written' "$preflight")" == 0 ]]
[[ "$(jq -er '.rows_written' "$apply")" == 1 ]]
[[ "$(jq -er '.rows_written' "$replay")" == 0 ]]
[[ "$(jq -er '[.outcomes[]|select(.status=="pending")]|length' "$apply")" == 1 ]]
[[ "$(jq -er '.cross_owner_claim_count' "$isolation")" == 0 ]]
[[ "$(jq -er '.cross_owner_outbox_count' "$isolation")" == 0 ]]
[[ "$(jq -er '.cross_owner_insert_rejected' "$isolation")" == true ]]

exact_state=$(
  docker exec "$container" psql -X -A -F '|' -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone_db" -c \
    "SELECT count(*),min(status::text),max(status::text),min(attempts),
       max(attempts),bool_and(available_at='infinity'::timestamptz)
     FROM memory.projection_outbox
     WHERE owner_user_id='$owner'::uuid
       AND aggregate_id=ANY(string_to_array('$claim_csv',',')::uuid[])"
)
[[ "$exact_state" == "1|pending|pending|0|0|t" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

report="$artifact_dir/report.json"
jq -n \
  --arg head "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg plan_sha256 "$expected_plan_sha" \
  --arg admission_result_sha256 "$(jq -er '.result_sha256' "$apply")" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:"memory_v1_v5_2_evidence_context_stance_projection_clone_report_v1",
    head_commit:$head,
    plan_sha256:$plan_sha256,
    owner_user_id:"1240822d-ac9a-4096-95aa-e2b24d36ef50",
    preflight_rows:0,
    apply_rows:1,
    replay_rows:0,
    exact_pending_held_outbox_rows:1,
    admission_result_sha256:$admission_result_sha256,
    cross_owner_claim_count:0,
    cross_owner_outbox_count:0,
    cross_owner_insert_rejected:true,
    qdrant_sha256:$qdrant_sha256,
    qdrant_unchanged:true,
    production_writes:0,
    external_model_calls:0,
    retrieval_activated:false,
    prompt_influence_activated:false
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
printf 'report=%s\n' "$report"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'memory_v1_v5_2_evidence_context_stance_projection_clone: PASS\n'
