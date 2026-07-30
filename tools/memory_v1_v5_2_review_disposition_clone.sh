#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Exercises six exact reviewed V5.2 dispositions in a
# disposable production clone. Production remains read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_v5_2_review_disposition_${$}"
migration=ops/sql/20260730_memory_v1_v5_2_review_disposition.sql
rollback=ops/sql/20260730_memory_v1_v5_2_review_disposition_rollback.sql
manifest=evals/memory_v1_v5_2_review_disposition_exact_six_20260730.json
backup=$(mktemp /tmp/memory-v5-2-review-disposition.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v5-2-review-disposition.XXXXXX)
chmod 0600 "$backup"
chmod 0700 "$work"

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

for file in "$migration" "$rollback" "$manifest"; do
  [[ -f "$file" ]]
done
bash -n "$0"
git diff --check
[[ -z "$(git status --short)" ]]
jq -e '
  .contract_version=="memory_v1_v5_2_review_disposition_manifest_v1" and
  (.items | length)==6 and
  ([.items[].packet_id] | unique | length)==6 and
  ([.items[].operation_id] | unique | length)==6 and
  ([.items[].disposition_id] | unique | length)==6 and
  ([.items[] | select(.review_decision=="rejected")] | length)==5 and
  ([.items[] | select(.review_decision=="deferred")] | length)==1
' "$manifest" >/dev/null

production_dispositions_before=$(scalar "$production" \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')
production_stage_before=$(scalar "$production" \
  'SELECT count(*) FROM memory.relational_stage_batch')
production_claims_before=$(scalar "$production" \
  'SELECT count(*) FROM memory.claim')
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" <"$backup"

clone_dispositions_before=$(scalar "$clone" \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')
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
    'memory.finalize_owner_v5_2_review_disposition_v1(
      uuid,uuid,uuid,text,text,text,text
    )'
  ) IS NULL)::int")" == 1 ]]
clone_sql <"$migration" >/dev/null

while IFS= read -r item; do
  owner=$(jq -r '.owner_user_id' <<<"$item")
  packet=$(jq -r '.packet_id' <<<"$item")
  packet_sha=$(jq -r '.packet_storage_sha256' <<<"$item")
  operation=$(jq -r '.operation_id' <<<"$item")
  disposition=$(jq -r '.disposition_id' <<<"$item")
  decision=$(jq -r '.review_decision' <<<"$item")
  reason=$(jq -r '.reason_code' <<<"$item")
  basis=$(jq -r '.review_basis_sha256' <<<"$item")
  output="$work/$packet.apply"
  replay="$work/$packet.replay"
  printf 'TEST_PACKET=%s\n' "$packet"

  psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
    -v owner="$owner" -v packet="$packet" -v packet_sha="$packet_sha" \
    -v operation="$operation" -v disposition="$disposition" \
    -v decision="$decision" -v reason="$reason" -v basis="$basis" \
    <<'SQL' >"$output"
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT *
FROM memory.finalize_owner_v5_2_review_disposition_v1(
  :'operation'::uuid,
  :'disposition'::uuid,
  :'packet'::uuid,
  :'packet_sha',
  :'decision',
  :'reason',
  :'basis'
);
COMMIT;
SQL
  grep -q 'applied' "$output"

  psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
    -v owner="$owner" -v packet="$packet" -v packet_sha="$packet_sha" \
    -v operation="$operation" -v disposition="$disposition" \
    -v decision="$decision" -v reason="$reason" -v basis="$basis" \
    <<'SQL' >"$replay"
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT *
FROM memory.finalize_owner_v5_2_review_disposition_v1(
  :'operation'::uuid,
  :'disposition'::uuid,
  :'packet'::uuid,
  :'packet_sha',
  :'decision',
  :'reason',
  :'basis'
);
COMMIT;
SQL
  grep -q 'replayed' "$replay"
done < <(jq -c '.items[]' "$manifest")

[[ "$(scalar "$clone" \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')" \
  == "$((clone_dispositions_before + 6))" ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE reason_code='semantic_predicate_misclassification'
    AND review_decision='rejected'
    AND NOT promotion_eligible
")" == 2 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE reason_code='turn_local_instruction_not_durable'
    AND review_decision='rejected'
    AND NOT promotion_eligible
")" == 2 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE reason_code='hypothetical_or_scenario_not_durable'
    AND review_decision='rejected'
    AND NOT promotion_eligible
")" == 1 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE reason_code='contextual_state_not_durable'
    AND review_decision='deferred'
    AND NOT promotion_eligible
")" == 1 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE packet_id='1f7fe393-afc3-5982-b69c-c90877659ef6'::uuid
")" == 0 ]]

first=$(jq -c '.items[0]' "$manifest")
owner=$(jq -r '.owner_user_id' <<<"$first")
other=9dd7426d-77eb-4765-9db2-13e33ad7444d
packet=$(jq -r '.packet_id' <<<"$first")
packet_sha=$(jq -r '.packet_storage_sha256' <<<"$first")
operation=$(jq -r '.operation_id' <<<"$first")
disposition=$(jq -r '.disposition_id' <<<"$first")
decision=$(jq -r '.review_decision' <<<"$first")
reason=$(jq -r '.reason_code' <<<"$first")
basis=$(jq -r '.review_basis_sha256' <<<"$first")
if psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
  -v owner="$other" -v packet="$packet" -v packet_sha="$packet_sha" \
  -v operation="$operation" -v disposition="$disposition" \
  -v decision="$decision" -v reason="$reason" -v basis="$basis" \
  <<'SQL' >/dev/null 2>&1
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT *
FROM memory.finalize_owner_v5_2_review_disposition_v1(
  :'operation'::uuid, :'disposition'::uuid, :'packet'::uuid, :'packet_sha',
  :'decision', :'reason', :'basis'
);
ROLLBACK;
SQL
then
  echo 'cross-owner reviewed disposition succeeded' >&2
  exit 1
fi

if clone_sql -c "
  UPDATE memory.v5_local_packet_disposition
  SET reason_code=reason_code
  WHERE disposition_id='d72c9b62-3f00-4dd0-8c71-05db3fde3b65'::uuid
" >/dev/null 2>&1; then
  echo 'append-only disposition accepted an update' >&2
  exit 1
fi

[[ "$(scalar "$clone" \
  'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$clone_stage_before" ]]
[[ "$(scalar "$clone" 'SELECT count(*) FROM memory.claim')" \
  == "$clone_claims_before" ]]
[[ "$(scalar "$production" \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')" \
  == "$production_dispositions_before" ]]
[[ "$(scalar "$production" \
  'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$production_stage_before" ]]
[[ "$(scalar "$production" 'SELECT count(*) FROM memory.claim')" \
  == "$production_claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf 'DISPOSITIONS=6\n'
printf 'REJECTED=5\n'
printf 'DEFERRED=1\n'
printf 'VALID_NAME_PACKET_UNCHANGED=1\n'
printf 'STAGE_WRITES=0\n'
printf 'CLAIM_WRITES=0\n'
printf 'QDRANT_WRITES=0\n'
printf 'CROSS_OWNER_VISIBLE=0\n'
printf 'ZERO_WRITE_REPLAY=proved\n'
printf 'memory_v1_v5_2_review_disposition_clone: PASS\n'
