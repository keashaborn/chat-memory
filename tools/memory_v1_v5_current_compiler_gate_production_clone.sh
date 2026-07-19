#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a production clone, installs the compiler-v3
# downstream gate, proves stale packets cannot plan, rolls back, and reinstalls.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

container=brains-postgres-1
production=memory
clone="memory_v5_compiler_gate_${$}"
migration=ops/sql/20260719_memory_v1_v5_current_compiler_gate.sql
rollback=ops/sql/20260719_memory_v1_v5_current_compiler_gate_rollback.sql
test_sql=tests/memory_v1_v5_current_compiler_gate.sql
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
expected_hash=5cb83e837e38174af4cfda016c20009168ff62974e7d9137b30228d9973256a2
backup=$(mktemp /tmp/memory-v1-compiler-gate.XXXXXX.dump)
dry=$(mktemp /tmp/memory-v1-compiler-gate-dry.XXXXXX.json)
chmod 0600 "$backup" "$dry"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$dry"
}
trap cleanup EXIT

clone_sql() {
  docker exec -i "$container" psql -U sage -d "$clone" -X \
    -v ON_ERROR_STOP=1 "$@"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

[[ -z "$(git status --porcelain)" ]]
[[ -f "$migration" && -f "$rollback" && -f "$test_sql" ]]
production_claims_before=$(docker exec "$container" psql -U sage -d \
  "$production" -X -Atqc 'SELECT count(*) FROM memory.claim')
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

clone_sql <"$migration" >/dev/null
clone_sql <"$migration" >/dev/null
clone_sql <"$test_sql" >/dev/null

clone_dsn=$(/opt/chat-memory/venv/bin/python -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  venv/bin/python scripts/memory_v1_v5_local_entity_validation.py \
    --owner-user-id "$owner" >"$dry"
jq -e '.apply==false and .database_writes==0 and .local_model_calls==0
  and .external_model_calls==0 and .plans[0].route=="no_work"' \
  "$dry" >/dev/null

clone_sql <"$rollback" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT strpos(pg_get_functiondef('memory.plan_owner_v5_local_entity_validation_v1(integer)'::regprocedure),'$expected_hash')")" == 0 ]]
clone_sql <"$migration" >/dev/null
clone_sql <"$test_sql" >/dev/null

[[ "$(docker exec "$container" psql -U sage -d "$production" -X -Atqc \
  'SELECT count(*) FROM memory.claim')" == "$production_claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
printf '%s\n' 'memory_v1_v5_current_compiler_gate_production_clone: PASS'
