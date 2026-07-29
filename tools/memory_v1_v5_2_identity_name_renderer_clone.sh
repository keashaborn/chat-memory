#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the identity-name renderer in a disposable
# production clone and reruns the exact three-observation claim-target review.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_claim_target_review_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
dahlia=917ab793-6f03-4af4-847b-c87f5632fa91
helsing=14e21c6b-1728-439b-9613-7d9b933d33b8
keasha=c0194481-bed5-438f-9407-07e398f14e50
python_bin=/opt/chat-memory/venv/bin/python
migration="$repo_root/ops/sql/20260729_memory_v1_v5_2_identity_name_renderer.sql"
rollback="$repo_root/ops/sql/20260729_memory_v1_v5_2_identity_name_renderer_rollback.sql"
security_test="$repo_root/tests/memory_v1_v5_2_identity_name_renderer.sql"
review_dir=/home/ubuntu/memory-v1-reviews
run_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
result="$review_dir/identity-name-renderer-review-$run_id.json"
backup=$(mktemp /tmp/memory-v5-2-identity-name-renderer.XXXXXX.dump)

set -a
if [[ -r "$repo_root/.env" ]]; then
  source "$repo_root/.env"
elif [[ -r /opt/chat-memory/.env ]]; then
  source /opt/chat-memory/.env
else
  echo 'POSTGRES_DSN environment file is unavailable' >&2
  exit 1
fi
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ "$(stat -c '%a' "$review_dir")" == 700 ]]
[[ ! -e "$result" ]]

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
  rm -f "$backup"
}
trap cleanup EXIT

production_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$source_db" -c "$1" | tr -d '[:space:]'
}

clone_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone_db" -c "$1" | tr -d '[:space:]'
}

protected_signature_sql="
  SELECT md5(jsonb_build_object(
    'entities',(SELECT count(*) FROM memory.entity),
    'observations',(SELECT count(*) FROM memory.observation),
    'bindings',(SELECT count(*) FROM memory.observation_entity_binding),
    'claims',(SELECT count(*) FROM memory.claim),
    'revisions',(SELECT count(*) FROM memory.claim_revision),
    'claim_links',(SELECT count(*) FROM memory.claim_observation),
    'projections',(SELECT count(*) FROM memory.projection_apply_event)
  )::text)
"

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

production_before=$(production_scalar "$protected_signature_sql")
qdrant_before=$(qdrant_signature)
head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec "$container" pg_dump -U sage -d "$source_db" -Fc >"$backup"
docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec -i "$container" pg_restore -U sage -d "$clone_db" <"$backup"
docker exec "$container" psql -X -U sage -d postgres -v ON_ERROR_STOP=1 \
  -c "COMMENT ON DATABASE \"$clone_db\" IS
      'memory_v1_v5_2_claim_target_review_clone_v1';" >/dev/null

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

source = urlsplit(os.environ["SOURCE_DSN"])
if source.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("unsupported PostgreSQL DSN scheme")
print(urlunsplit((
    source.scheme,
    source.netloc,
    "/" + os.environ["CLONE_DB"],
    source.query,
    source.fragment,
)))
PY
)

clone_before=$(clone_scalar "$protected_signature_sql")
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" <"$migration"
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" <"$security_test" >/dev/null

POSTGRES_DSN="$clone_dsn" MEMORY_V1_DISPOSABLE_CLONE_REQUIRED=1 \
PYTHONPATH="$repo_root:$repo_root/scripts" \
"$python_bin" "$repo_root/scripts/memory_v1_v5_2_claim_target_review.py" \
  --owner "$owner" \
  --observation "$dahlia" \
  --observation "$helsing" \
  --observation "$keasha" \
  --output "$result"

[[ "$(jq -er '.item_count' "$result")" == 3 ]]
[[ "$(jq -er '.action_counts.create' "$result")" == 1 ]]
[[ "$(jq -er '.action_counts.manual_review' "$result")" == 1 ]]
[[ "$(jq -er '.action_counts.reinforce' "$result")" == 1 ]]
[[ "$(jq -er --arg id "$dahlia" \
  '.items[]|select(.observation_id==$id)|.action' "$result")" == reinforce ]]
[[ "$(jq -er --arg id "$keasha" \
  '.items[]|select(.observation_id==$id)|.action' "$result")" == manual_review ]]
[[ "$(jq -cer --arg id "$keasha" \
  '.items[]|select(.observation_id==$id)|.reason_codes' "$result")" \
  == '["existing_semantic_aggregate_render_drift"]' ]]
[[ "$(jq -er --arg id "$helsing" \
  '.items[]|select(.observation_id==$id)|.action' "$result")" == create ]]
[[ "$(clone_scalar "$protected_signature_sql")" == "$clone_before" ]]

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" <"$rollback"
legacy_text=$(docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" -c "
    SET SESSION AUTHORIZATION brains_app;
    SELECT set_config(
      'app.user_id',
      '$owner',
      false
    );
    SELECT payload->>'canonical_text'
    FROM memory.expected_projection_payload_v5_2('$keasha');
    RESET SESSION AUTHORIZATION;
  " | sed -n '3p')
[[ "$legacy_text" == \
  "Keasha von Steffen Haus' name is Keasha von Steffen Haus." ]]

[[ "$(clone_scalar "$protected_signature_sql")" == "$clone_before" ]]
[[ "$(production_scalar "$protected_signature_sql")" == "$production_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$head_before" ]]

printf '%s\n' \
  'IDENTITY_NAME_RENDERER_CLONE=PASS' \
  "RESULT=$result" \
  'ACTIONS=create:1,reinforce:1,manual_review:1' \
  'PROTECTED_ROW_DELTAS=0' \
  'QDRANT_WRITES=0' \
  'PRODUCTION_WRITES=0'
