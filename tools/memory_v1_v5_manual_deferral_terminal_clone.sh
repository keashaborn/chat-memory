#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Verifies review-requested but structurally unstageable
# deferral packets in a disposable production clone. Qdrant is read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_v5_manual_deferral_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=5c4594d6-314d-5629-8f71-ba469074ab25
migration=ops/sql/20260719_memory_v1_v5_manual_deferral_terminal.sql
rollback=ops/sql/20260719_memory_v1_v5_manual_deferral_terminal_rollback.sql
test_sql=tests/memory_v1_v5_manual_deferral_terminal.sql
worker=scripts/memory_v1_v5_local_packet_router.py
worker_test=scripts/memory_v1_v5_local_packet_disposition_test.py
backup=$(mktemp /tmp/memory-v1-v5-manual-deferral.XXXXXX.dump)
worker_output=$(mktemp /tmp/memory-v1-v5-manual-deferral.XXXXXX.json)

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$worker_output"
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
[[ -f "$worker" && -f "$worker_test" ]]
[[ -z "$(git status --porcelain)" ]]
qdrant_before=$(qdrant_signature)
production_rows_before=$(docker exec "$container" psql -X -A -t \
  -U sage -d "$production" \
  -c 'SELECT count(*) FROM memory.v5_local_packet_disposition' \
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
    AND manual_review_required AND entity_mention_count=0
    AND observation_count=0 AND comparison_hint_count=0
    AND deferral_count>0")
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

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$rollback" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration" >/dev/null

set -a
source .env
set +a
clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone" python3 - <<'PY'
import os
from urllib.parse import urlsplit,urlunsplit
value=urlsplit(os.environ['SOURCE_DSN'])
print(urlunsplit((value.scheme,value.netloc,'/'+os.environ['CLONE_DB'],value.query,value.fragment)))
PY
)
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$worker_test" \
  >/dev/null
POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_V5_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_local_packet_router_apply_v1 \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --apply >"$worker_output"
jq -e '.apply==true and .outcome=="terminal_no_stage" and
  .write_counts.packet_route_events==1 and
  .write_counts.restricted_review_artifacts==0 and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .zero_write_replay_proved==true and .external_model_calls==0' \
  "$worker_output" >/dev/null

[[ "$(clone_scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == "$((rows_before+1))" ]]
[[ "$(clone_scalar "SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND reason_code='deferral_only_review_unresolved'")" == 1 ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$production" \
  -c 'SELECT count(*) FROM memory.v5_local_packet_disposition' \
  | tr -d '[:space:]')" == "$production_rows_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' 'memory_v1_v5_manual_deferral_terminal_clone: PASS'
