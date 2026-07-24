#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs and exercises the V5.2 packet router in a
# disposable production clone. Production Postgres and Qdrant remain read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_v5_2_router_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
migration=ops/sql/20260724_memory_v1_v5_2_local_packet_router.sql
rollback=ops/sql/20260724_memory_v1_v5_2_local_packet_router_rollback.sql
security_test=tests/memory_v1_v5_2_local_packet_router_security.sql
unit_test=tests/test_memory_v1_v5_2_local_packet_router.py
worker=scripts/memory_v1_v5_2_local_packet_router.py
backup=$(mktemp /tmp/memory-v5-2-router.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v5-2-router.XXXXXX)
reviews="$work/reviews"
mkdir -m 0700 "$reviews"
chmod 0600 "$backup"
role_preexisting=0

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  if [[ "$role_preexisting" == 0 ]]; then
    docker exec "$container" psql -X -U sage -d "$production" \
      -v ON_ERROR_STOP=1 \
      -c 'DROP ROLE IF EXISTS memory_v5_2_local_router_maintainer' \
      >/dev/null 2>&1 || true
  fi
  rm -f "$backup"
  rm -rf "$work"
}
trap cleanup EXIT

clone_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" -c "$1" | tr -d '[:space:]'
}

production_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$production" -c "$1" | tr -d '[:space:]'
}

run_clone_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" "$@"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for file in \
  "$migration" "$rollback" "$security_test" "$unit_test" "$worker"
do
  [[ -f "$file" ]]
done
bash -n "$0"
git diff --check
[[ -z "$(git status --short)" ]]

if [[ "$(production_scalar \
  "SELECT (to_regrole('memory_v5_2_local_router_maintainer') IS NOT NULL)::int")" \
  == 1 ]]; then
  role_preexisting=1
fi
[[ "$role_preexisting" == 0 ]]

qdrant_before=$(qdrant_signature)
production_stage_before=$(production_scalar \
  'SELECT count(*) FROM memory.relational_stage_batch')
production_claims_before=$(production_scalar \
  'SELECT count(*) FROM memory.claim')
production_dispositions_before=$(production_scalar \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')
[[ "$(production_scalar \
  "SELECT (to_regclass('memory.v5_2_local_packet_route_event') IS NULL)::int")" \
  == 1 ]]

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --no-owner <"$backup"

stage_before=$(clone_scalar 'SELECT count(*) FROM memory.relational_stage_batch')
claims_before=$(clone_scalar 'SELECT count(*) FROM memory.claim')
dispositions_before=$(clone_scalar \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')

set -a
source /opt/chat-memory/.env
set +a
clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone" python3 - <<'PY'
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

run_clone_sql <"$migration" >/dev/null
run_clone_sql <"$migration" >/dev/null

target_packet=$(POSTGRES_DSN="$clone_dsn" \
  /opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import os
import asyncpg

OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"

async def main():
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
            value = await conn.fetchval(
                "SELECT packet_id::text "
                "FROM memory.plan_owner_v5_2_local_packet_route_v1(1)"
            )
            if value is None:
                raise RuntimeError("production clone has no eligible V5.2 packet")
            print(value)
    finally:
        await conn.close()

asyncio.run(main())
PY
)

psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
  -v target_packet_id="$target_packet" <"$security_test" >/dev/null
[[ "$(clone_scalar \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" == 0 ]]

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$unit_test"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import os
import uuid

import asyncpg

from scripts.memory_v1_v5_2_local_packet_router import plan_owner

OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")

async def main():
    conn = await asyncpg.connect(
        os.environ["POSTGRES_DSN"], command_timeout=30, ssl=False
    )
    try:
        assert await conn.fetchval("SELECT session_user") == "brains_app"
        target = await plan_owner(conn, OWNER)
        if target is None:
            raise RuntimeError("V5.2 router direct planner returned no work")
    finally:
        await conn.close()

asyncio.run(main())
PY

dry_output="$work/dry.json"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --review-root "$reviews" >"$dry_output"
jq -e '
  .apply==false and .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0 and
  (.plans | length)==1 and .plans[0].route!="no_work"
' "$dry_output" >/dev/null

terminal_seen=0
review_seen=0
for ordinal in $(seq 1 20); do
  output="$work/apply-${ordinal}.json"
  POSTGRES_DSN="$clone_dsn" \
  MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_2_local_packet_router_apply_v1 \
  PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$worker" \
    --owner-user-id "$owner" --review-root "$reviews" --apply >"$output"
  jq -e '
    .apply==true and .zero_write_replay_proved==true and
    .external_model_calls==0 and
    .write_counts.stage==0 and .write_counts.claims==0 and
    .write_counts.qdrant==0 and .write_counts.prompt_influence==0
  ' "$output" >/dev/null
  outcome=$(jq -r '.outcome' "$output")
  [[ "$outcome" != no_work ]]
  if [[ "$outcome" == terminal_no_stage ]]; then
    terminal_seen=1
  elif [[ "$outcome" == manual_review_artifact_ready ]]; then
    review_seen=1
  fi
  if [[ "$terminal_seen" == 1 && "$review_seen" == 1 ]]; then
    break
  fi
done
[[ "$terminal_seen" == 1 ]]
[[ "$review_seen" == 1 ]]

event_count=$(clone_scalar \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')
[[ "$event_count" -ge 2 ]]
[[ "$(clone_scalar \
  "SELECT count(*) FROM memory.v5_2_local_packet_route_event
   WHERE route='terminal_no_stage'")" -ge 1 ]]
[[ "$(clone_scalar \
  "SELECT count(*) FROM memory.v5_2_local_packet_route_event
   WHERE route='manual_review_artifact_ready'
     AND review_contract='memory_v1_v5_2_local_packet_review_v1'
     AND bundle_contract='memory_v1_v5_2_stage_preflight_v1'")" -ge 1 ]]

other_output="$work/other.json"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$other" --review-root "$reviews" >"$other_output"
target_packet_sha=$(printf '%s' "$target_packet" | sha256sum | awk '{print $1}')
if jq -e --arg target "$target_packet_sha" \
  '.plans[].packet_id_sha256==$target' "$other_output" >/dev/null; then
  echo 'cross-owner V5.2 packet was exposed' >&2
  exit 1
fi

event_id=$(clone_scalar \
  'SELECT route_event_id FROM memory.v5_2_local_packet_route_event LIMIT 1')
if psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
  -c "SELECT set_config('app.user_id','$owner',false);
      UPDATE memory.v5_2_local_packet_route_event
      SET reason_code=reason_code WHERE route_event_id='$event_id'::uuid" \
  >/dev/null 2>&1; then
  echo 'append-only V5.2 route event accepted an update' >&2
  exit 1
fi

[[ "$(clone_scalar 'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$stage_before" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" \
  == "$dispositions_before" ]]

run_clone_sql <"$rollback" >/dev/null
[[ "$(clone_scalar \
  "SELECT (to_regclass('memory.v5_2_local_packet_route_event') IS NULL)::int")" \
  == 1 ]]
[[ "$(clone_scalar \
  "SELECT (to_regrole('memory_v5_2_local_router_maintainer') IS NULL)::int")" \
  == 1 ]]

[[ "$(production_scalar 'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$production_stage_before" ]]
[[ "$(production_scalar 'SELECT count(*) FROM memory.claim')" \
  == "$production_claims_before" ]]
[[ "$(production_scalar \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')" \
  == "$production_dispositions_before" ]]
[[ "$(production_scalar \
  "SELECT (to_regclass('memory.v5_2_local_packet_route_event') IS NULL)::int")" \
  == 1 ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' 'memory_v1_v5_2_local_packet_router_production_clone: PASS'
