#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs and exercises exact duplicate packet
# supersession in a disposable production clone. Production is read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_v5_2_duplicate_authority_${$}"
owner=9dd7426d-77eb-4765-9db2-13e33ad7444d
other=1240822d-ac9a-4096-95aa-e2b24d36ef50
evidence=30ffeb73-3c76-4e84-a9cd-6e50f89a5afe
prior=98868708-3acf-5243-9447-3515f06d83be
replacement=b962f49b-73f8-5a10-8f07-afbe9d0c54dc
prior_sha=865ff509b5694511a4d581eef057e6b421c7c296ec05a356784648fe8b3918bb
replacement_sha=4e640f21cada7a01a5ecb133109e2d9a78c1da3247ebae0e0c05b3ee79cbadf7
operation=28bc15e4-1220-59f7-bbb8-8b1a190d3a68
supersession=1a60c02f-d64a-5bc5-9934-54be7a413d8c
reason=duplicate_active_packet_reconciled
migration=ops/sql/20260730_memory_v1_v5_2_duplicate_packet_authority.sql
rollback=ops/sql/20260730_memory_v1_v5_2_duplicate_packet_authority_rollback.sql
security_test=tests/memory_v1_v5_2_duplicate_packet_authority.sql
worker=scripts/memory_v1_v5_2_local_packet_router.py
backup=$(mktemp /tmp/memory-v5-2-duplicate-authority.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v5-2-duplicate-authority.XXXXXX)
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

clone_sql() {
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

for file in "$migration" "$rollback" "$security_test" "$worker"; do
  [[ -f "$file" ]]
done
bash -n "$0"
git diff --check
[[ -z "$(git status --short)" ]]

production_supersessions_before=$(scalar "$production" \
  'SELECT count(*) FROM memory.v5_local_packet_supersession')
production_routes_before=$(scalar "$production" \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')
production_stage_before=$(scalar "$production" \
  'SELECT count(*) FROM memory.relational_stage_batch')
production_claims_before=$(scalar "$production" \
  'SELECT count(*) FROM memory.claim')
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" <"$backup"

clone_supersessions_before=$(scalar "$clone" \
  'SELECT count(*) FROM memory.v5_local_packet_supersession')
clone_routes_before=$(scalar "$clone" \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')
clone_stage_before=$(scalar "$clone" \
  'SELECT count(*) FROM memory.relational_stage_batch')
clone_claims_before=$(scalar "$clone" \
  'SELECT count(*) FROM memory.claim')

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

clone_sql <"$migration" >/dev/null
clone_sql <"$migration" >/dev/null
clone_sql <"$rollback" >/dev/null
[[ "$(scalar "$clone" \
  "SELECT (to_regprocedure(
    'memory.plan_owner_v5_2_duplicate_packet_supersession_v1(uuid,uuid)'
   ) IS NULL)::int")" == 1 ]]
clone_sql <"$migration" >/dev/null

psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
  -v owner_user_id="$owner" \
  -v other_owner_user_id="$other" \
  -v evidence_id="$evidence" \
  -v prior_packet_id="$prior" \
  -v replacement_packet_id="$replacement" \
  -v prior_packet_storage_sha256="$prior_sha" \
  -v replacement_packet_storage_sha256="$replacement_sha" \
  <"$security_test" >/dev/null
[[ "$(scalar "$clone" \
  'SELECT count(*) FROM memory.v5_local_packet_supersession')" \
  == "$clone_supersessions_before" ]]

psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
  -v owner="$owner" \
  -v prior="$prior" \
  -v replacement="$replacement" \
  -v prior_sha="$prior_sha" \
  -v replacement_sha="$replacement_sha" \
  -v operation="$operation" \
  -v supersession="$supersession" \
  -v reason="$reason" <<'SQL' >/dev/null
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT *
FROM memory.finalize_owner_v5_2_duplicate_packet_supersession_v1(
  :'operation'::uuid,
  :'supersession'::uuid,
  :'prior'::uuid,
  :'replacement'::uuid,
  :'prior_sha',
  :'replacement_sha',
  :'reason'
);
COMMIT;
SQL

[[ "$(scalar "$clone" \
  'SELECT count(*) FROM memory.v5_local_packet_supersession')" \
  == "$((clone_supersessions_before + 1))" ]]

dry="$work/dry.json"
apply="$work/apply.json"
replay="$work/replay.json"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --packet-id "$replacement" \
  --review-root "$reviews" >"$dry"
jq -e '
  .apply==false and (.plans | length)==1 and
  .plans[0].route=="manual_review_artifact_ready" and
  .database_writes==0 and .stage_writes==0 and .claim_writes==0 and
  .qdrant_writes==0 and .external_model_calls==0 and
  .prompt_influence==0
' "$dry" >/dev/null

POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_2_local_packet_router_apply_v1 \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --packet-id "$replacement" \
  --review-root "$reviews" --apply >"$apply"
jq -e '
  .apply==true and .outcome=="manual_review_artifact_ready" and
  .zero_write_replay_proved==true and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .external_model_calls==0
' "$apply" >/dev/null

POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_2_local_packet_router_apply_v1 \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --packet-id "$replacement" \
  --review-root "$reviews" --apply >"$replay"
jq -e '
  .apply==true and .outcome=="no_work" and
  .write_counts.route_events==0 and .write_counts.stage==0 and
  .write_counts.claims==0 and .write_counts.qdrant==0 and
  .write_counts.prompt_influence==0 and .external_model_calls==0
' "$replay" >/dev/null

other_output="$work/other.json"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$other" --packet-id "$replacement" \
  --review-root "$reviews" >"$other_output"
jq -e '
  .apply==false and (.plans | length)==1 and
  .plans[0].route=="no_work" and .database_writes==0
' "$other_output" >/dev/null

[[ "$(scalar "$clone" \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" \
  == "$((clone_routes_before + 1))" ]]
[[ "$(scalar "$clone" \
  'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$clone_stage_before" ]]
[[ "$(scalar "$clone" 'SELECT count(*) FROM memory.claim')" \
  == "$clone_claims_before" ]]

if clone_sql -c "
  SELECT set_config('app.user_id','$owner',false);
  UPDATE memory.v5_local_packet_supersession
  SET reason_code=reason_code
  WHERE supersession_id='$supersession'::uuid
" >/dev/null 2>&1; then
  echo 'append-only supersession accepted an update' >&2
  exit 1
fi

[[ "$(scalar "$production" \
  'SELECT count(*) FROM memory.v5_local_packet_supersession')" \
  == "$production_supersessions_before" ]]
[[ "$(scalar "$production" \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" \
  == "$production_routes_before" ]]
[[ "$(scalar "$production" \
  'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$production_stage_before" ]]
[[ "$(scalar "$production" 'SELECT count(*) FROM memory.claim')" \
  == "$production_claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf 'SUPERSESSIONS=1\n'
printf 'ROUTES=1\n'
printf 'STAGE_WRITES=0\n'
printf 'CLAIM_WRITES=0\n'
printf 'QDRANT_WRITES=0\n'
printf 'CROSS_OWNER_VISIBLE=0\n'
printf 'ZERO_WRITE_REPLAY=proved\n'
printf 'memory_v1_v5_2_duplicate_packet_authority_clone: PASS\n'
