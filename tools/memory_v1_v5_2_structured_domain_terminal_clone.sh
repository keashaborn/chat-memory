#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Rehearses the exact V5.2 structured-domain terminal
# disposition path in a disposable production clone. Production remains read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_v5_2_structured_terminal_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=a24c2166-ec0d-558c-ab96-9e1ca74c97a2
observation_packet=e21e39bb-20fe-5b95-bc72-77fd774f0faf
migration=ops/sql/20260723_memory_v1_v5_2_structured_domain_terminal.sql
rollback=ops/sql/20260723_memory_v1_v5_2_structured_domain_terminal_rollback.sql
security_test=tests/memory_v1_v5_2_structured_domain_terminal.sql
downstream_isolation_test=tests/memory_v1_predicate_runtime_v5_1_downstream_isolation.sql
worker=scripts/memory_v1_v5_local_packet_disposition.py
backup=$(mktemp /tmp/memory-v5-2-structured-terminal.XXXXXX.dump)
dry_output=$(mktemp /tmp/memory-v5-2-structured-terminal-dry.XXXXXX.json)
apply_output=$(mktemp /tmp/memory-v5-2-structured-terminal-apply.XXXXXX.json)
other_output=$(mktemp /tmp/memory-v5-2-structured-terminal-other.XXXXXX.json)
chmod 0600 "$backup" "$dry_output" "$apply_output" "$other_output"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$dry_output" "$apply_output" "$other_output"
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

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for file in \
  "$migration" "$rollback" "$security_test" "$downstream_isolation_test" "$worker"
do
  [[ -f "$file" ]]
done
bash -n "$0"

qdrant_before=$(qdrant_signature)
production_dispositions_before=$(production_scalar \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')
production_stage_before=$(production_scalar \
  'SELECT count(*) FROM memory.relational_stage_batch')
production_claims_before=$(production_scalar \
  'SELECT count(*) FROM memory.claim')

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --no-owner <"$backup"

rows_before=$(clone_scalar \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')
review_before=$(clone_scalar \
  'SELECT count(*) FROM memory.v5_local_packet_review_artifact')
stage_before=$(clone_scalar \
  'SELECT count(*) FROM memory.relational_stage_batch')
claims_before=$(clone_scalar 'SELECT count(*) FROM memory.claim')

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

packet_storage_sha256=$(clone_scalar "SELECT packet_storage_sha256
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")
observation_packet_storage_sha256=$(clone_scalar \
  "SELECT packet_storage_sha256
   FROM memory.evidence_extraction_packet_v5_local
   WHERE owner_user_id='$owner'::uuid
     AND packet_id='$observation_packet'::uuid")

[[ "$(psql "$clone_dsn" -X -A -t -v ON_ERROR_STOP=1 \
  -c "SELECT set_config('app.user_id','$owner',false);
      SELECT count(*) FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
      WHERE packet_id='$packet'::uuid" | tail -n 1 | tr -d '[:space:]')" == 0 ]]

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration" >/dev/null
[[ "$(psql "$clone_dsn" -X -A -t -v ON_ERROR_STOP=1 \
  -c "SELECT set_config('app.user_id','$owner',false);
      SELECT count(*) FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
      WHERE packet_id='$packet'::uuid
        AND disposition_route='terminal_deferral'
        AND reason_code='deferral_only_no_stage'" \
  | tail -n 1 | tr -d '[:space:]')" == 1 ]]

# Rollback is valid before a V5.2 terminal disposition exists.
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$rollback" >/dev/null
[[ "$(psql "$clone_dsn" -X -A -t -v ON_ERROR_STOP=1 \
  -c "SELECT set_config('app.user_id','$owner',false);
      SELECT count(*) FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
      WHERE packet_id='$packet'::uuid" | tail -n 1 | tr -d '[:space:]')" == 0 ]]
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration" >/dev/null

psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" -v other_owner="$other" \
  -v packet_id="$packet" \
  -v packet_storage_sha256="$packet_storage_sha256" \
  -v observation_packet_id="$observation_packet" \
  -v observation_packet_storage_sha256="$observation_packet_storage_sha256" \
  <"$security_test" >/dev/null
[[ "$(clone_scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == "$rows_before" ]]

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" \
  -v target_owner="$owner" -v other_owner="$other" \
  <"$downstream_isolation_test" >/dev/null
[[ "$(clone_scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == "$rows_before" ]]

packet_sha256=$(printf '%s' "$packet" | sha256sum | awk '{print $1}')
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" >"$dry_output"
jq -e --arg packet_sha256 "$packet_sha256" '
  .apply==false and .database_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0 and
  .plans[0].packet_id_sha256==$packet_sha256 and
  .plans[0].route=="terminal_deferral" and
  .plans[0].reason_code=="deferral_only_no_stage" and
  .plans[0].counts=={
    entity_mentions:0,observations:0,comparison_hints:0,deferrals:1
  }' "$dry_output" >/dev/null

POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_V5_LOCAL_PACKET_DISPOSITION_APPLY=memory_v1_v5_local_packet_disposition_apply_v1 \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --apply >"$apply_output"
jq -e --arg packet_sha256 "$packet_sha256" '
  .apply==true and .outcome=="terminal_no_stage" and
  .plans[0].packet_id_sha256==$packet_sha256 and
  .write_counts.dispositions==1 and .write_counts.stage==0 and
  .write_counts.claims==0 and .write_counts.qdrant==0 and
  .write_counts.prompt_influence==0 and
  .zero_write_replay_proved==true and .external_model_calls==0
' "$apply_output" >/dev/null

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$other" >"$other_output"
if jq -e --arg packet_sha256 "$packet_sha256" \
  '.plans[].packet_id_sha256==$packet_sha256' "$other_output" >/dev/null; then
  echo 'cross-owner structured-domain packet was exposed' >&2
  exit 1
fi

[[ "$(clone_scalar "SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND disposition='terminal_no_stage'
    AND reason_code='deferral_only_no_stage'
    AND review_decision IS NULL
    AND NOT promotion_eligible
    AND review_basis_sha256 IS NULL")" == 1 ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == "$((rows_before+1))" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.v5_local_packet_review_artifact')" == "$review_before" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.relational_stage_batch')" == "$stage_before" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]

[[ "$(production_scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == "$production_dispositions_before" ]]
[[ "$(production_scalar 'SELECT count(*) FROM memory.relational_stage_batch')" == "$production_stage_before" ]]
[[ "$(production_scalar 'SELECT count(*) FROM memory.claim')" == "$production_claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' 'memory_v1_v5_2_structured_domain_terminal_clone: PASS'
