#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run through sudo" >&2
  exit 2
fi

repo_root=$(
  cd "$(dirname "${BASH_SOURCE[0]}")/.."
  pwd
)
container=brains-postgres-1
production=memory
clone="memory_context_unavailable_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad
sql="$repo_root/tools/memory_v1_legacy_context_unavailable_finalize_v1.sql"
backup=$(mktemp /tmp/memory-context-unavailable.XXXXXX.dump)
before=$(mktemp /tmp/memory-context-unavailable-before.XXXXXX)
after=$(mktemp /tmp/memory-context-unavailable-after.XXXXXX)
replay=$(mktemp /tmp/memory-context-unavailable-replay.XXXXXX)
production_before=$(mktemp /tmp/memory-context-unavailable-production-before.XXXXXX)
production_after=$(mktemp /tmp/memory-context-unavailable-production-after.XXXXXX)

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f \
    "$backup" "$before" "$after" "$replay" \
    "$production_before" "$production_after"
  exit "$rc"
}
trap cleanup EXIT

state_query="
  SELECT 'claim',count(*) FROM memory.claim
  UNION ALL SELECT 'entity',count(*) FROM memory.entity
  UNION ALL SELECT 'evidence_extraction_event',count(*)
    FROM memory.evidence_extraction_event
  UNION ALL SELECT 'evidence_extraction_job',count(*)
    FROM memory.evidence_extraction_job
  UNION ALL SELECT 'evidence_extraction_packet_v5_local',count(*)
    FROM memory.evidence_extraction_packet_v5_local
  UNION ALL SELECT 'observation',count(*) FROM memory.observation
  UNION ALL SELECT 'v5_local_inference_event',count(*)
    FROM memory.v5_local_inference_event
  UNION ALL SELECT 'vantage_answer_trace',count(*)
    FROM public.vantage_answer_trace
  ORDER BY 1
"

db_state() {
  local database=$1
  docker exec "$container" psql -U sage -d "$database" -X -At -F $'\t' \
    -c "$state_query"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H "content-type: application/json" \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll |
    jq -cS '.result.points | sort_by(.id | tostring)' |
    sha256sum |
    awk '{print $1}'
}

run_finalize() {
  local database=$1
  local expected_total=$2
  local expected_deferred=$3
  local expected_superseded=$4
  local expected_deleted=$5
  docker exec -i "$container" psql -U sage -d "$database" -X \
    -P pager=off -v ON_ERROR_STOP=1 \
    -v owner_user_id="$owner" \
    -v expected_total="$expected_total" \
    -v expected_deferred="$expected_deferred" \
    -v expected_superseded="$expected_superseded" \
    -v expected_deleted="$expected_deleted" \
    <"$sql"
}

cd "$repo_root"
test -f "$sql"
test -z "$(git status --porcelain)"
test "$(systemctl is-active brains.service)" = active
test "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" = active

set -a
source /opt/chat-memory/.env
set +a
test -n "${POSTGRES_DSN:-}"

db_state "$production" >"$production_before"
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc --no-owner --no-privileges >"$backup"
test -s "$backup"
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"
docker exec "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 -c \
  'GRANT USAGE ON SCHEMA memory TO brains_app;
   GRANT SELECT ON memory.evidence_extraction_job TO brains_app;
   GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app' \
  >/dev/null

clone_dsn=$(
  /opt/chat-memory/venv/bin/python -c \
    'import sys; from urllib.parse import urlsplit,urlunsplit; p=urlsplit(sys.argv[1]); print(urlunsplit((p.scheme,p.netloc,"/"+sys.argv[2],p.query,p.fragment)))' \
    "$POSTGRES_DSN" "$clone"
)

db_state "$clone" >"$before"
run_finalize "$clone" 114 7 3 104
db_state "$clone" >"$after"

join -t $'\t' "$before" "$after" |
awk -F '\t' '
  $1=="evidence_extraction_event" {
    if (($3-$2)!=114) exit 1
    next
  }
  $2!=$3 {exit 1}
'

docker exec -i "$container" psql -U sage -d "$clone" -X -At \
  -v ON_ERROR_STOP=1 -v owner="$owner" <<'SQL' >"$replay"
SELECT count(*)
FROM memory.evidence_extraction_job
WHERE owner_user_id=:'owner'::uuid
  AND selector_version='20260717_v2'
  AND result->'final'->'payload'->>'contract_version'
      ='memory_v1_legacy_context_unavailable_finalize_v1';
SELECT count(*)
FROM memory.evidence_extraction_job
WHERE owner_user_id=:'owner'::uuid
  AND selector_version='20260717_v2'
  AND attempts=0
  AND status='skipped'
  AND result->'final'->'payload'->>'disposition'='deferred'
  AND result->'final'->'payload'->>'reason_code'
      ='context_unavailable';
SELECT count(*)
FROM memory.evidence_extraction_job
WHERE owner_user_id=:'owner'::uuid
  AND selector_version='20260717_v2'
  AND attempts=0
  AND status='skipped'
  AND result->'final'->'payload'->>'disposition'='skipped'
  AND result->'final'->'payload'->>'reason_code'
      ='superseded_by_existing_processing';
SELECT count(*)
FROM memory.evidence_extraction_job
WHERE owner_user_id=:'owner'::uuid
  AND selector_version='20260717_v2'
  AND attempts=0
  AND status='skipped'
  AND result->'final'->'payload'->>'disposition'='skipped'
  AND result->'final'->'payload'->>'reason_code'
      ='source_evidence_deleted';
SELECT count(*)
FROM memory.evidence_extraction_event
WHERE owner_user_id=:'owner'::uuid
  AND actor_ref='memory_v1_legacy_context_unavailable_finalize_v1';
SQL

mapfile -t verified <"$replay"
test "${verified[0]}" = 114
test "${verified[1]}" = 7
test "${verified[2]}" = 3
test "${verified[3]}" = 104
test "${verified[4]}" = 114

replay_before=$(sha256sum "$after" | awk '{print $1}')
run_finalize "$clone" 0 0 0 0 >/dev/null
db_state "$clone" >"$replay"
test "$replay_before" = "$(sha256sum "$replay" | awk '{print $1}')"

zero_candidate_report=$(run_finalize "$clone" 0 0 0 0)
grep -q '"candidate_count": 0' <<<"$zero_candidate_report"

cross_owner=$(
  psql "$clone_dsn" -X -At -v ON_ERROR_STOP=1 \
    -v owner="$other_owner" -v target_owner="$owner" <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id', :'owner', true);
SELECT count(*)
FROM memory.evidence_extraction_job
WHERE owner_user_id=:'target_owner'::uuid
  AND selector_version='20260717_v2';
ROLLBACK;
SQL
)
test "$(printf '%s\n' "$cross_owner" | grep -E '^[0-9]+$' | tail -1)" = 0

db_state "$production" >"$production_after"
cmp -s "$production_before" "$production_after"
test "$qdrant_before" = "$(qdrant_signature)"

echo "LEGACY_CONTEXT_UNAVAILABLE_CLONE=PASS candidates=114 deferred=7 superseded=3 deleted=104"
