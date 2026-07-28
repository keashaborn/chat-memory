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
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad
service=memory-v1-v5-local-inference-scheduler.service
timer=memory-v1-v5-local-inference-scheduler.timer
sql="$repo_root/tools/memory_v1_legacy_context_unavailable_finalize_v1.sql"
expected_commit=${EXPECTED_PRODUCTION_COMMIT:?}
expected_sql_sha256=${EXPECTED_SQL_SHA256:?}
run_tag=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="/var/backups/chat-memory/context-unavailable-${run_tag}"
review_dir="/home/ubuntu/memory-v1-reviews/context-unavailable-${run_tag}"
before="$review_dir/protected-before.tsv"
after="$review_dir/protected-after.tsv"
replay="$review_dir/protected-replay.tsv"
apply_report="$review_dir/apply.txt"
replay_report="$review_dir/replay.txt"

timer_enabled=$(systemctl is-enabled "$timer" 2>/dev/null || true)
timer_active=$(systemctl is-active "$timer" 2>/dev/null || true)

restore_timer() {
  if [[ $timer_enabled == enabled ]]; then
    systemctl enable "$timer" >/dev/null
  else
    systemctl disable "$timer" >/dev/null 2>&1 || true
  fi
  if [[ $timer_active == active ]]; then
    systemctl start "$timer"
  else
    systemctl stop "$timer" >/dev/null 2>&1 || true
  fi
}

cleanup() {
  rc=$?
  trap - EXIT
  restore_timer
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
  local expected_total=$1
  local expected_deferred=$2
  local expected_superseded=$3
  local expected_deleted=$4
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
test "$(git rev-parse HEAD)" = "$expected_commit"
test -z "$(git status --porcelain)"
test "$(sha256sum "$sql" | awk '{print $1}')" = "$expected_sql_sha256"
test "$(systemctl is-active brains.service)" = active
test "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" = active

mkdir -p "$backup_dir" "$review_dir"
chmod 0700 "$backup_dir" "$review_dir"

systemctl stop "$timer"
for _ in $(seq 1 120); do
  [[ $(systemctl is-active "$service" 2>/dev/null || true) != activating ]] \
    && break
  sleep 1
done
test "$(systemctl is-active "$service" 2>/dev/null || true)" != activating

db_state >"$before"
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_dir/postgres-before.dump"
test -s "$backup_dir/postgres-before.dump"
sha256sum "$backup_dir/postgres-before.dump" \
  >"$backup_dir/postgres-before.dump.sha256"

run_finalize 114 7 3 104 | tee "$apply_report"
db_state >"$after"

join -t $'\t' "$before" "$after" |
awk -F '\t' '
  $1=="evidence_extraction_event" {
    if (($3-$2)!=114) exit 1
    next
  }
  $2!=$3 {exit 1}
'

run_finalize 0 0 0 0 | tee "$replay_report" >/dev/null
db_state >"$replay"
cmp -s "$after" "$replay"

mapfile -t verified < <(
  docker exec -i "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 -v owner="$owner" <<'SQL'
SELECT count(*)
FROM memory.evidence_extraction_job
WHERE owner_user_id=:'owner'::uuid
  AND selector_version='20260717_v2'
  AND attempts=0
  AND status='skipped'
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
  AND result->'final'->'payload'->>'reason_code'
      ='superseded_by_existing_processing';
SELECT count(*)
FROM memory.evidence_extraction_job
WHERE owner_user_id=:'owner'::uuid
  AND selector_version='20260717_v2'
  AND attempts=0
  AND status='skipped'
  AND result->'final'->'payload'->>'reason_code'
      ='source_evidence_deleted';
SQL
)
test "${verified[0]}" = 114
test "${verified[1]}" = 7
test "${verified[2]}" = 3
test "${verified[3]}" = 104

set -a
source /opt/chat-memory/.env
set +a
cross_owner=$(
  psql "$POSTGRES_DSN" -X -At -v ON_ERROR_STOP=1 \
    -v owner="$other_owner" -v target_owner="$owner" <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id', :'owner', true);
SELECT count(*)
FROM memory.evidence_extraction_job
WHERE owner_user_id=:'target_owner'::uuid
  AND result->'final'->'payload'->>'contract_version'
      ='memory_v1_legacy_context_unavailable_finalize_v1';
ROLLBACK;
SQL
)
test "$(printf '%s\n' "$cross_owner" | grep -E '^[0-9]+$' | tail -1)" = 0

test "$qdrant_before" = "$(qdrant_signature)"
test "$(systemctl is-active brains.service)" = active
test "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" = active

sha256sum "$sql" "$apply_report" "$replay_report" "$before" "$after" \
  >"$review_dir/artifact-sha256s.txt"

restore_timer
trap - EXIT

test "$(systemctl is-enabled "$timer" 2>/dev/null || true)" = "$timer_enabled"
test "$(systemctl is-active "$timer" 2>/dev/null || true)" = "$timer_active"

echo "LEGACY_CONTEXT_UNAVAILABLE_PRODUCTION=PASS candidates=114 deferred=7 superseded=3 deleted=104"
echo "BACKUP=$backup_dir/postgres-before.dump"
echo "REVIEW=$review_dir"
