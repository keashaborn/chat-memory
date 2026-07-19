#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Verifies the zero-call terminal-deferral compatibility
# migration against a fresh disposable clone of production.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_v5_zero_call_${$}"
owner=557ea042-cb82-48f8-9429-472e96c957ef
other=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
packet=70dc2e9e-27fa-588d-a54b-966c466949af
migration=ops/sql/20260719_memory_v1_v5_local_packet_disposition_zero_call_compat.sql
rollback=ops/sql/20260719_memory_v1_v5_local_packet_disposition_zero_call_compat_rollback.sql
test_sql=tests/memory_v1_v5_local_packet_disposition_zero_call_compat.sql
backup=$(mktemp /tmp/memory-v1-v5-zero-call-clone.XXXXXX.dump)
qdrant_before=

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists "$clone" >/dev/null 2>&1 || true
  rm -f "$backup"
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

clone_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" -c "$1" | tr -d '[:space:]'
}

[[ -f "$migration" && -f "$rollback" && -f "$test_sql" ]]
[[ -z "$(git status --porcelain)" ]]
qdrant_before=$(qdrant_signature)
production_claims_before=$(docker exec "$container" psql -X -A -t \
  -U sage -d "$production" -c 'SELECT count(*) FROM memory.claim' \
  | tr -d '[:space:]')

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --no-owner --no-privileges <"$backup"

packet_sha=$(clone_scalar "SELECT packet_storage_sha256
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND local_model_calls=0 AND external_model_calls=0")
[[ "$packet_sha" =~ ^[0-9a-f]{64}$ ]]
rows_before=$(clone_scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')
claims_before=$(clone_scalar 'SELECT count(*) FROM memory.claim')

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" \
  -v owner_user_id="$owner" -v other_owner_user_id="$other" \
  -v packet_id="$packet" -v packet_storage_sha256="$packet_sha" \
  <"$test_sql" >/dev/null
[[ "$(clone_scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == "$rows_before" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$rollback" >/dev/null
strict=$(clone_scalar "SELECT pg_get_constraintdef(oid)
  FROM pg_constraint
  WHERE conrelid='memory.v5_local_packet_disposition'::regclass
    AND conname='v5_local_packet_disposition_local_model_calls_check'")
[[ "$strict" == 'CHECK((local_model_calls=1))' ]]
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration" >/dev/null

owner_name=$(clone_scalar "SELECT pg_get_userbyid(proowner)
  FROM pg_proc WHERE oid=
  'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'::regprocedure")
[[ "$owner_name" == sage ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == "$rows_before" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$production" \
  -c 'SELECT count(*) FROM memory.claim' | tr -d '[:space:]')" == "$production_claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' 'memory_v1_v5_local_packet_disposition_zero_call_compat_clone: PASS'
