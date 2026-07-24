#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Exercises deferred projection-outbox admission for the
# reconciled stance in a disposable production clone. Production and Qdrant are
# read-only; no embedding or answer-influence path is invoked.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor \
  ab5a83e9c3f62c835a82e1742b2b2d003c3ee258 HEAD
head=$(git -C "$repo_root" rev-parse HEAD)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_deferred_projection_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
claim_id=8fb8b3ab-a627-4555-99c6-fe4dc9b0ca89
source_dir=/home/ubuntu/memory-v1-reviews/reconciled-production-20260724T185638Z_ab5a83e9c3f6
apply_result="$source_dir/materialize-apply.json"
apply_manifest="$source_dir/apply-manifest.json"
runner=scripts/memory_v1_v5_deferred_projection_admission.py
python_bin=/opt/chat-memory/venv/bin/python
artifact_dir="/home/ubuntu/memory-v1-reviews/deferred-projection-clone-$(date -u +%Y%m%dT%H%M%SZ)-${head:0:12}"

[[ -f "$apply_result" && -f "$apply_manifest" ]]
[[ "$(stat -c '%a' "$apply_result")" == 600 ]]
[[ "$(stat -c '%a' "$apply_manifest")" == 600 ]]
required_head=$(jq -er '.required_head_commit' "$apply_manifest")
[[ "$required_head" == ab5a83e9c3f62c835a82e1742b2b2d003c3ee258 ]]
[[ -x "$python_bin" && -f "$repo_root/$runner" ]]
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
[[ "$(docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" -c \
  "WITH removed AS (
     DELETE FROM memory.projection_outbox
     WHERE owner_user_id='$owner'::uuid
       AND aggregate_id='$claim_id'::uuid
     RETURNING 1
   ) SELECT count(*) FROM removed")" == 1 ]]
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

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root/scripts" \
  "$python_bin" - "$owner" "$other_owner" "$claim_id" "$isolation" <<'PY'
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

import asyncpg


async def main() -> None:
    owner = uuid.UUID(sys.argv[1])
    other = str(uuid.UUID(sys.argv[2]))
    claim = uuid.UUID(sys.argv[3])
    output = Path(sys.argv[4])
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        tx = conn.transaction(readonly=True, isolation="serializable")
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", other)
        claim_count = await conn.fetchval(
            "SELECT count(*) FROM memory.claim WHERE owner_user_id=$1 AND claim_id=$2",
            owner,
            claim,
        )
        outbox_count = await conn.fetchval(
            """SELECT count(*) FROM memory.projection_outbox
               WHERE owner_user_id=$1 AND aggregate_id=$2""",
            owner,
            claim,
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
                json.dumps({"claim_id": str(uuid.uuid4()), "revision_number": 2}),
            )
        except (asyncpg.InsufficientPrivilegeError, asyncpg.CheckViolationError):
            insert_rejected = True
        finally:
            await tx.rollback()
        if claim_count != 0 or outbox_count != 0 or not insert_rejected:
            raise RuntimeError("cross-owner projection isolation failed")
        output.write_text(
            json.dumps(
                {
                    "cross_owner_claim_count": claim_count,
                    "cross_owner_outbox_count": outbox_count,
                    "cross_owner_insert_rejected": insert_rejected,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        output.chmod(0o600)
    finally:
        await conn.close()


asyncio.run(main())
PY

[[ "$(jq -er '.rows_written' "$preflight")" == 0 ]]
[[ "$(jq -er '.rows_written' "$apply")" == 1 ]]
[[ "$(jq -er '.rows_written' "$replay")" == 0 ]]
[[ "$(jq -er '.outcomes[0].status' "$apply")" == pending ]]
[[ "$(jq -er '.cross_owner_claim_count' "$isolation")" == 0 ]]
[[ "$(jq -er '.cross_owner_outbox_count' "$isolation")" == 0 ]]
[[ "$(jq -er '.cross_owner_insert_rejected' "$isolation")" == true ]]
outbox_id=$(jq -er '.outcomes[0].outbox_id' "$apply")
exact_state=$(
  docker exec "$container" psql -X -A -F '|' -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone_db" -c \
    "SELECT count(*),min(status::text),min(attempts),
       bool_and(available_at='infinity'::timestamptz)
     FROM memory.projection_outbox
     WHERE owner_user_id='$owner'::uuid
       AND aggregate_id='$claim_id'::uuid
       AND outbox_id='$outbox_id'::uuid
       AND payload=jsonb_build_object(
         'claim_id','$claim_id','revision_number',2
       )"
)
[[ "$exact_state" == "1|pending|0|t" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

report="$artifact_dir/report.json"
jq -n \
  --arg head "$head" \
  --arg claim_id "$claim_id" \
  --arg outbox_id "$outbox_id" \
  --arg qdrant_sha256 "$qdrant_after" \
  --arg apply_result_sha256 "$(jq -er '.result_sha256' "$apply")" \
  '{
    contract_version:"memory_v1_v5_deferred_projection_admission_clone_report_v1",
    head_commit:$head,
    claim_id:$claim_id,
    outbox_id:$outbox_id,
    preflight_rows:0,
    apply_rows:1,
    replay_rows:0,
    cross_owner_claim_count:0,
    cross_owner_outbox_count:0,
    cross_owner_insert_rejected:true,
    qdrant_sha256:$qdrant_sha256,
    qdrant_unchanged:true,
    apply_result_sha256:$apply_result_sha256,
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
printf 'memory_v1_v5_deferred_projection_admission_production_clone: PASS\n'
