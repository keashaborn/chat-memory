#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Verifies authenticated-owner discovery, deterministic
# owner rotation, and cross-owner packet isolation in a disposable production
# clone. Production Postgres and Qdrant remain read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_v5_2_universal_router_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
worker=scripts/memory_v1_v5_2_local_packet_router.py
unit_router=tests/test_memory_v1_v5_2_local_packet_router.py
unit_owners=tests/test_memory_v1_authenticated_owners.py
service=ops/systemd/memory-v1-v5-2-local-packet-router.service
backup=$(mktemp /tmp/memory-v5-2-universal-router.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v5-2-universal-router.XXXXXX)
reviews="$work/reviews"
mkdir -m 0700 "$reviews"
chmod 0600 "$backup"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
}
trap cleanup EXIT

scalar() {
  local database=$1
  local query=$2
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$query" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for file in "$worker" "$unit_router" "$unit_owners" "$service"; do
  [[ -f "$file" ]]
done
bash -n "$0"
git diff --check
[[ -z "$(git status --short)" ]]
! grep -q -- '--owner-user-id' "$service"

production_routes_before=$(scalar "$production" \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')
production_stage_before=$(scalar "$production" \
  'SELECT count(*) FROM memory.relational_stage_batch')
production_claims_before=$(scalar "$production" \
  'SELECT count(*) FROM memory.claim')
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" <"$backup"

clone_routes_before=$(scalar "$clone" \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')
clone_stage_before=$(scalar "$clone" \
  'SELECT count(*) FROM memory.relational_stage_batch')
clone_claims_before=$(scalar "$clone" \
  'SELECT count(*) FROM memory.claim')
owner_count=$(scalar "$clone" \
  "SELECT count(*) FROM memory.authenticated_owner_registry_v1
   WHERE last_verified_at >= clock_timestamp() - interval '90 days'")
[[ "$owner_count" -ge 2 ]]

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

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  -m unittest \
  tests.test_memory_v1_v5_2_local_packet_router \
  tests.test_memory_v1_authenticated_owners

dry_output="$work/dry.json"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --review-root "$reviews" >"$dry_output"
jq -e --argjson owners "$owner_count" '
  .apply==false and .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0 and
  (.plans | length)==$owners and
  ([.plans[].owner_user_id_sha256] | unique | length)==$owners
' "$dry_output" >/dev/null

admin_packet=$(scalar "$clone" \
  "SELECT packet_id FROM memory.v5_2_local_packet_route_event
   WHERE owner_user_id='$owner'::uuid
   ORDER BY created_at LIMIT 1")
[[ -n "$admin_packet" ]]

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  OWNER="$other" PACKET="$admin_packet" \
  /opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import os
import uuid

import asyncpg

from scripts.memory_v1_v5_2_local_packet_router import plan_owner


async def main() -> None:
    conn = await asyncpg.connect(
        os.environ["POSTGRES_DSN"],
        command_timeout=30,
        ssl=False,
    )
    try:
        result = await plan_owner(
            conn,
            uuid.UUID(os.environ["OWNER"]),
            uuid.UUID(os.environ["PACKET"]),
        )
        if result is not None:
            raise RuntimeError("cross-owner exact packet became visible")
    finally:
        await conn.close()


asyncio.run(main())
PY

[[ "$(scalar "$clone" \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" \
  == "$clone_routes_before" ]]
[[ "$(scalar "$clone" \
  'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$clone_stage_before" ]]
[[ "$(scalar "$clone" 'SELECT count(*) FROM memory.claim')" \
  == "$clone_claims_before" ]]

[[ "$(scalar "$production" \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" \
  == "$production_routes_before" ]]
[[ "$(scalar "$production" \
  'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$production_stage_before" ]]
[[ "$(scalar "$production" 'SELECT count(*) FROM memory.claim')" \
  == "$production_claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf 'AUTHENTICATED_OWNERS=%s\n' "$owner_count"
printf 'DATABASE_WRITES=0\n'
printf 'QDRANT_WRITES=0\n'
printf 'CROSS_OWNER_VISIBLE=0\n'
printf 'memory_v1_v5_2_universal_router_clone: PASS\n'
