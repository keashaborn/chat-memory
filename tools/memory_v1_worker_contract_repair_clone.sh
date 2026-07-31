#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores current production into a disposable clone and
# exercises the two failed worker targets. Production and Qdrant are read-only.

if [[ "$(id -u)" -ne 0 ]]; then
  echo 'run through sudo; root is required for the disposable clone' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_worker_contract_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=1c61fb73-4deb-5053-ba14-b6dcac6af309
assessment=87b96ed7-b1aa-5597-9aaf-2ffc90ba87bd
observation=58584a02-d35e-44bb-86cd-ca1e18475a80
migration=ops/sql/20260731_memory_v1_worker_contract_repair.sql
rollback=ops/sql/20260731_memory_v1_worker_contract_repair_rollback.sql
security_test=tests/memory_v1_worker_contract_repair_security.sql
unit_test=tests/test_memory_v1_v5_local_claim_projection_v2.py
packet_worker=scripts/memory_v1_v5_2_local_packet_router.py
claim_worker=scripts/memory_v1_v5_local_claim_projection.py
backup=$(mktemp /tmp/memory-worker-contract.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-worker-contract.XXXXXX)
review_root="$work/reviews"
mkdir -m 0700 "$review_root"
chmod 0600 "$backup"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
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
  "$migration" "$rollback" "$security_test" "$unit_test" \
  "$packet_worker" "$claim_worker"
do
  [[ -f "$file" ]]
done
bash -n "$0"
git diff --check
PYTHONPATH="$repo_root:$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$unit_test"

set -a
source /opt/chat-memory/.env
set +a

qdrant_before=$(qdrant_signature)
production_claims_before=$(production_scalar \
  'SELECT count(*) FROM memory.claim')
production_plans_before=$(production_scalar \
  'SELECT count(*) FROM memory.projection_plan')
production_routes_before=$(production_scalar \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')

echo 'phase=create_clone'
docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --no-owner <"$backup"

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone" \
  /opt/chat-memory/venv/bin/python - <<'PY'
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

actor_scalar() {
  psql "$clone_dsn" -X -q -A -t -v ON_ERROR_STOP=1 \
    -c "BEGIN READ ONLY;
        SELECT set_config('app.user_id','$owner',true);
        $1
        ROLLBACK;" \
    | tail -n 1 | tr -d '[:space:]'
}

claims_before=$(clone_scalar 'SELECT count(*) FROM memory.claim')
plans_before=$(clone_scalar 'SELECT count(*) FROM memory.projection_plan')
outbox_before=$(clone_scalar 'SELECT count(*) FROM memory.projection_outbox')
routes_before=$(clone_scalar \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')

echo 'phase=install_and_security'
run_clone_sql <"$migration" >/dev/null
run_clone_sql <"$migration" >/dev/null
echo 'phase=rollback_reinstall'
run_clone_sql <"$rollback" >/dev/null
[[ "$(clone_scalar \
  "SELECT (to_regclass(
     'memory.v5_local_claim_projection_terminal_v1'
   ) IS NULL)::int")" == 1 ]]
[[ "$(clone_scalar \
  "SELECT (to_regprocedure(
     'memory.register_owner_v5_local_claim_projection_v2(
       uuid,uuid,uuid,uuid,text,text,text,text
     )'
   ) IS NULL)::int")" == 1 ]]
run_clone_sql <"$migration" >/dev/null
run_clone_sql \
  -v owner_user_id="$owner" \
  -v packet_id="$packet" \
  <"$security_test" >/dev/null

[[ "$(actor_scalar \
  "SELECT count(*) FROM memory.plan_owner_v5_2_exact_packet_route_v1(
     '$packet'::uuid
   );")" == 1 ]]
[[ "$(clone_scalar \
  "SELECT count(*) FROM memory.v5_local_claim_projection_terminal_v1")" == 0 ]]

echo 'phase=packet_worker'
packet_output="$work/packet.json"
POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_2_local_packet_router_apply_v1 \
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$packet_worker" \
  --owner-user-id "$owner" --packet-id "$packet" \
  --review-root "$review_root" --apply >"$packet_output"
jq -e '
  .apply==true and .outcome=="terminal_no_stage" and
  .zero_write_replay_proved==true and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .external_model_calls==0
' "$packet_output" >/dev/null
[[ "$(actor_scalar \
  "SELECT count(*) FROM memory.plan_owner_v5_2_exact_packet_route_v1(
     '$packet'::uuid
   );")" == 0 ]]

echo 'phase=claim_worker'
claim_output="$work/claim.json"
if ! POSTGRES_DSN="$clone_dsn" \
  MEMORY_V1_V5_LOCAL_CLAIM_PROJECTION_APPLY=memory_v1_v5_local_claim_projection_apply_v1 \
  PYTHONPATH="$repo_root:$repo_root/scripts" \
    /opt/chat-memory/venv/bin/python "$claim_worker" \
    --owner-user-id "$owner" --apply >"$claim_output"
then
  jq -cS . "$claim_output" >&2 || true
  exit 1
fi
if ! jq -e '
  .apply==true and .outcome=="already_materialized" and
  .database_rows_created==1 and .zero_write_replay_proved==true and
  .write_counts.already_materialized_terminal==1 and
  .write_counts.admission==0 and .write_counts.projection_plan_rows==0 and
  .write_counts.claims==0 and .write_counts.qdrant==0 and
  .write_counts.prompt_influence==0 and
  .external_model_calls==0 and .local_model_calls==0
' "$claim_output" >/dev/null
then
  jq -cS . "$claim_output" >&2
  exit 1
fi
[[ "$(clone_scalar \
  "SELECT count(*) FROM memory.v5_local_claim_projection_terminal_v1
   WHERE owner_user_id='$owner'::uuid
     AND assessment_id='$assessment'::uuid
     AND observation_id='$observation'::uuid
     AND decision='already_materialized'")" == 1 ]]
[[ "$(actor_scalar \
  "SELECT count(*) FROM memory.plan_owner_v5_local_claim_projection_v1(20)
   WHERE assessment_id='$assessment'::uuid;")" == 0 ]]

echo 'phase=isolation_and_append_only'
run_clone_sql >/dev/null <<SQL
BEGIN READ ONLY;
SET LOCAL ROLE memory_v5_local_projection_maintainer;
SELECT set_config('app.user_id','$other',true);
DO \$isolation\$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.v5_local_claim_projection_terminal_v1
    WHERE owner_user_id='$owner'::uuid
  ) THEN
    RAISE EXCEPTION 'cross-owner terminal record is visible';
  END IF;
END
\$isolation\$;
ROLLBACK;
SQL

terminal_id=$(clone_scalar \
  "SELECT terminal_id
   FROM memory.v5_local_claim_projection_terminal_v1
   WHERE owner_user_id='$owner'::uuid")
if run_clone_sql \
  -c "BEGIN;
      SET LOCAL ROLE memory_v5_local_projection_maintainer;
      SELECT set_config('app.user_id','$owner',true);
      UPDATE memory.v5_local_claim_projection_terminal_v1
      SET decision=decision WHERE terminal_id='$terminal_id'::uuid;
      ROLLBACK;" \
  >/dev/null 2>&1; then
  echo 'append-only terminal row accepted an update' >&2
  exit 1
fi

[[ "$(clone_scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.projection_plan')" \
  == "$plans_before" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.projection_outbox')" \
  == "$outbox_before" ]]
echo 'phase=production_and_qdrant_unchanged'
[[ "$(production_scalar 'SELECT count(*) FROM memory.claim')" \
  == "$production_claims_before" ]]
[[ "$(production_scalar 'SELECT count(*) FROM memory.projection_plan')" \
  == "$production_plans_before" ]]
[[ "$(production_scalar \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" \
  == "$production_routes_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' 'memory_v1_worker_contract_repair_clone: PASS'
