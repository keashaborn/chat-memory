#!/usr/bin/env bash
set -euo pipefail

live=/opt/chat-memory
target=/tmp/chat-memory-pet-temporal-integration-v2
expected_old=c7a2ed4aa29560038165d996ce673712318b6120
expected_new=${MEMORY_V1_TRUSTED_TEMPORAL_EXPECTED_NEW:-}
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad
job=707a8ee4-6ff8-4a25-aed2-b06797bbd1eb
evidence=88161526-0c53-5291-8601-0bd0ba41da53
content_sha=ad1715dd9f2680ca992dd0d51a07869db9d3947c8d9e835ef1720f129eb035da
prior_failure=c342aa2a-c58b-5549-84fc-b93bafa083dc
completion=f5caa02c-38c7-4e09-ac3e-77658af73e8a
prior_compiler=f82e6f4339dfe4aada7e5c3edb71fde8125a819f33677b3f47b98f3726b60419
operation=9ead4394-78dc-5dde-889a-b33118484fd4
run_id=654735e7-6c22-54b2-854a-4caded09efe9
contract=memory_v1_trusted_temporal_boundary_compiler_repair_v1
migration=ops/sql/20260729_memory_v1_trusted_temporal_retry.sql
rollback=ops/sql/20260729_memory_v1_trusted_temporal_retry_rollback.sql
migration_sha=af8b7746928dfc1d7c9e1be8a9262659e09b9a55c2ad78dc2e496fc2fa48c091
rollback_sha=0d921f70451e76e3f4609fb59f0f5da9dccc96364feaf8ea4b8938b411c7e0a0
canary=scripts/memory_v1_v5_local_inference_canary.py
api_key_file=/etc/memory-v1-local-inference/api-key
python_bin=/opt/chat-memory/venv/bin/python
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir=/var/backups/chat-memory/trusted-temporal-retry-"$timestamp"
artifact_dir=$(mktemp -d /tmp/memory-trusted-temporal-production.XXXXXX)
timer_state=$artifact_dir/timers.tsv
table_list=$artifact_dir/tables.txt
static_before=$artifact_dir/static-before.tsv
static_after=$artifact_dir/static-after.tsv
isolation_before=$artifact_dir/isolation-before.tsv
isolation_after=$artifact_dir/isolation-after.tsv
qdrant_before=$artifact_dir/qdrant-before.sha
qdrant_after=$artifact_dir/qdrant-after.sha
canary_output=$artifact_dir/canary.json
canary_log=$artifact_dir/canary.log
timers_restored=0
phase=preflight

psql_scalar() {
  psql "$POSTGRES_DSN" -X -Atq -v ON_ERROR_STOP=1 -c "$1"
}

restore_timers() {
  if [[ "$timers_restored" == 1 || ! -s "$timer_state" ]]; then
    return
  fi
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    else
      systemctl disable "$unit" >/dev/null 2>&1 || true
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
  done <"$timer_state"
  timers_restored=1
}

cleanup() {
  rc=$?
  trap - EXIT
  restore_timers
  if [[ "$rc" != 0 ]]; then
    printf 'memory_v1_trusted_temporal_retry_production: FAIL phase=%s rc=%s\n' \
      "$phase" "$rc" >&2
    if [[ -s "$canary_output" ]]; then
      jq -c '{
        outcome,
        rejection_code,
        local_model_calls,
        external_model_calls,
        write_counts
      }' "$canary_output" >&2 || true
    fi
    if [[ -s "$canary_log" ]]; then
      tail -40 "$canary_log" >&2 || true
    fi
    printf 'artifacts=%s\n' "$artifact_dir" >&2
  else
    rm -rf "$artifact_dir"
  fi
  exit "$rc"
}
trap cleanup EXIT

capture_static() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    test "$table" != evidence_extraction_job
    test "$table" != evidence_extraction_event
    test "$table" != v5_local_inference_event
    test "$table" != evidence_extraction_packet_v5_local
    state=$(
      docker exec "$container" psql -U sage -d "$database" -X -Atqc "
        SELECT count(*)::text||E'\\t'||encode(public.digest(
          convert_to(coalesce(string_agg(row_json,E'\\n'
            ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
        FROM (
          SELECT to_jsonb(value)::text AS row_json
          FROM memory.\"$table\" AS value
        ) AS rows;
      "
    )
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
}

capture_isolation() {
  local output=$1
  docker exec "$container" psql -U sage -d "$database" -X -Atq \
    -v ON_ERROR_STOP=1 >"$output" <<SQL
WITH state(label,row_json) AS (
  SELECT 'other_jobs',to_jsonb(value)::text
  FROM memory.evidence_extraction_job AS value
  WHERE owner_user_id<>'$owner'::uuid
  UNION ALL
  SELECT 'other_events',to_jsonb(value)::text
  FROM memory.evidence_extraction_event AS value
  WHERE owner_user_id<>'$owner'::uuid
  UNION ALL
  SELECT 'other_ledger',to_jsonb(value)::text
  FROM memory.v5_local_inference_event AS value
  WHERE owner_user_id<>'$owner'::uuid
  UNION ALL
  SELECT 'other_packets',to_jsonb(value)::text
  FROM memory.evidence_extraction_packet_v5_local AS value
  WHERE owner_user_id<>'$owner'::uuid
  UNION ALL
  SELECT 'same_owner_other_jobs',to_jsonb(value)::text
  FROM memory.evidence_extraction_job AS value
  WHERE owner_user_id='$owner'::uuid AND job_id<>'$job'::uuid
  UNION ALL
  SELECT 'same_owner_other_events',to_jsonb(value)::text
  FROM memory.evidence_extraction_event AS value
  WHERE owner_user_id='$owner'::uuid AND job_id<>'$job'::uuid
  UNION ALL
  SELECT 'same_owner_other_ledger',to_jsonb(value)::text
  FROM memory.v5_local_inference_event AS value
  WHERE owner_user_id='$owner'::uuid AND job_id IS DISTINCT FROM '$job'::uuid
  UNION ALL
  SELECT 'same_owner_other_packets',to_jsonb(value)::text
  FROM memory.evidence_extraction_packet_v5_local AS value
  WHERE owner_user_id='$owner'::uuid AND job_id<>'$job'::uuid
), labels(label) AS (VALUES
  ('other_jobs'),
  ('other_events'),
  ('other_ledger'),
  ('other_packets'),
  ('same_owner_other_jobs'),
  ('same_owner_other_events'),
  ('same_owner_other_ledger'),
  ('same_owner_other_packets')
)
SELECT label||E'\t'||count(row_json)::text||E'\t'||
  encode(public.digest(convert_to(coalesce(string_agg(
    row_json,E'\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
FROM labels LEFT JOIN state USING(label)
GROUP BY label ORDER BY label;
SQL
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

test "$(id -u)" -eq 0
test "$expected_new" != ""
test "$(git -C "$live" rev-parse HEAD)" = "$expected_old"
test -z "$(git -C "$live" status --porcelain)"
test -z "$(git -C "$target" status --porcelain)"
test "$(git -C "$target" rev-parse HEAD)" = "$expected_new"
git -C "$live" merge-base --is-ancestor "$expected_old" "$expected_new"
test "$(sha256sum "$target/$migration" | awk '{print $1}')" = "$migration_sha"
test "$(sha256sum "$target/$rollback" | awk '{print $1}')" = "$rollback_sha"
test -r "$api_key_file"
test "$(stat -c %a "$api_key_file")" = 600

set -a
source "$live/.env"
set +a
test -n "${POSTGRES_DSN:-}"

systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u \
  | while read -r unit; do
      printf '%s\t%s\t%s\n' "$unit" \
        "$(systemctl is-enabled "$unit")" \
        "$(systemctl is-active "$unit")"
    done >"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
test "$(
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE lease_token IS NOT NULL
      AND lease_expires_at > clock_timestamp();
  "
)" = 0

phase=fresh_backup
install -d -m 0700 "$backup_dir"
docker exec "$container" pg_dump -U sage -d "$database" -Fc \
  >"$backup_dir/memory.dump.partial"
test -s "$backup_dir/memory.dump.partial"
mv "$backup_dir/memory.dump.partial" "$backup_dir/memory.dump"
chmod 0600 "$backup_dir/memory.dump"
printf '%s\n' "$expected_old" >"$backup_dir/rollback-head.txt"
cp "$target/$rollback" "$backup_dir/rollback.sql"
chmod 0600 "$backup_dir/rollback.sql"
sha256sum "$backup_dir/memory.dump" "$backup_dir/rollback.sql" \
  >"$backup_dir/SHA256SUMS"

phase=baseline
docker exec "$container" psql -U sage -d "$database" -X -Atq -c "
  SELECT table_name
  FROM information_schema.tables
  WHERE table_schema='memory'
    AND table_type='BASE TABLE'
    AND table_name NOT IN (
      'evidence_extraction_job',
      'evidence_extraction_event',
      'v5_local_inference_event',
      'evidence_extraction_packet_v5_local'
    )
  ORDER BY table_name;
" >"$table_list"
capture_static "$static_before"
capture_isolation "$isolation_before"
qdrant_signature >"$qdrant_before"

phase=code_fast_forward
git -C "$live" tag "rollback/trusted-temporal-retry-$timestamp" "$expected_old"
git -C "$live" merge --ff-only "$expected_new"
test -z "$(git -C "$live" status --porcelain)"

phase=migration_install
docker exec -i "$container" psql -U sage -d "$database" \
  -X -v ON_ERROR_STOP=1 <"$live/$migration" >/dev/null
test "$(
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    SELECT pg_get_userbyid(proowner)||E'\t'||
      has_function_privilege(
        'brains_app',
        oid,
        'EXECUTE'
      )::text||E'\t'||
      has_function_privilege(
        'public',
        oid,
        'EXECUTE'
      )::text
    FROM pg_proc
    WHERE oid=
      'memory.requeue_owner_trusted_temporal_failure_v1(
        uuid,uuid,text,uuid,uuid,integer,text,text
      )'::regprocedure;
  "
)" = $'memory_v5_local_inference_maintainer\ttrue\tfalse'

phase=cross_owner_rejection
if psql "$POSTGRES_DSN" -X -q -v ON_ERROR_STOP=1 \
    >/dev/null 2>&1 <<SQL
BEGIN;
SELECT set_config('app.user_id','$other_owner',true);
SELECT *
FROM memory.requeue_owner_trusted_temporal_failure_v1(
  '$operation','$job','$content_sha','$prior_failure','$completion',2,
  '$prior_compiler','$contract'
);
ROLLBACK;
SQL
then
  echo 'cross-owner retry unexpectedly succeeded' >&2
  exit 1
fi

phase=reviewed_retry_apply
apply_result=$(
  psql "$POSTGRES_DSN" -X -Atq -v ON_ERROR_STOP=1 <<SQL | tail -1
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT status||E'\t'||attempts::text||E'\t'||apply_outcome
FROM memory.requeue_owner_trusted_temporal_failure_v1(
  '$operation','$job','$content_sha','$prior_failure','$completion',2,
  '$prior_compiler','$contract'
);
COMMIT;
SQL
)
test "$apply_result" = pending$'\t'2$'\t'applied

events_after_apply=$(
  psql_scalar "
    SELECT count(*)
    FROM memory.evidence_extraction_event
    WHERE owner_user_id='$owner'::uuid
      AND operation_id='$operation'::uuid;
  "
)
replay_result=$(
  psql "$POSTGRES_DSN" -X -Atq -v ON_ERROR_STOP=1 <<SQL | tail -1
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT status||E'\t'||attempts::text||E'\t'||apply_outcome
FROM memory.requeue_owner_trusted_temporal_failure_v1(
  '$operation','$job','$content_sha','$prior_failure','$completion',2,
  '$prior_compiler','$contract'
);
ROLLBACK;
SQL
)
test "$replay_result" = pending$'\t'2$'\t'replayed
test "$(
  psql_scalar "
    SELECT count(*)
    FROM memory.evidence_extraction_event
    WHERE owner_user_id='$owner'::uuid
      AND operation_id='$operation'::uuid;
  "
)" = "$events_after_apply"

phase=private_canary
set +e
MEMORY_V1_V5_LOCAL_INFERENCE_APPLY=memory_v1_v5_local_inference_canary_apply_v1 \
MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(<"$api_key_file")" \
PYTHONPATH="$live" \
  "$python_bin" "$live/$canary" \
    --owner-user-id "$owner" \
    --evidence-id "$evidence" \
    --expected-job-id "$job" \
    --expected-content-sha256 "$content_sha" \
    --selector-version 20260729_v4_contextual_resplit \
    --contract-profile v5_2 \
    --max-attempts 3 \
    --max-output-tokens 2048 \
    --max-reserved-jobs 100 \
    --failure-threshold 10 \
    --run-id "$run_id" \
    --apply >"$canary_output" 2>"$canary_log"
canary_rc=$?
set -e
test "$canary_rc" = 0
jq -e '
  .outcome=="accepted"
  and .manual_review_required==true
  and .local_model_calls==1
  and .external_model_calls==0
  and .zero_write_replay_proved==true
  and .write_counts.claims==0
  and .write_counts.qdrant==0
  and .write_counts.prompt_influence==0
' "$canary_output" >/dev/null

phase=semantic_verification
psql_scalar "
  SELECT normalized_packet::text
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid;
" >"$artifact_dir/packet.json"
"$python_bin" - "$artifact_dir/packet.json" <<'PY'
import json
from pathlib import Path
import sys

packet = json.loads(Path(sys.argv[1]).read_text())
entities = [
    item for item in packet["entity_mentions"]
    if item.get("name_text") == "Helsing"
]
assert len(entities) == 1
refs = {item["entity_ref"] for item in entities}
deaths = [
    item for item in packet["observations"]
    if item["predicate"] == "life_event.died"
]
assert len(deaths) == 1
assert deaths[0]["subject_entity_ref"] in refs
temporal = deaths[0]["temporal"]
assert temporal["basis"] == "relative"
assert temporal["shape"] == "instant"
assert temporal["source_form"] == "relative"
assert temporal["anchored_to_source_time"] is False
assert temporal["relative_offset"]["anchor_source"] == \
    "evidence_observed_at"
PY

test "$(
  psql_scalar "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND job_id='$job'::uuid
      AND status='review_required'
      AND attempts=3
      AND last_error IS NULL;
  "
)" = 1
test "$(
  psql_scalar "
    SELECT count(*)
    FROM memory.evidence_extraction_event
    WHERE owner_user_id='$owner'::uuid
      AND operation_id='$operation'::uuid
      AND event_type='queued'
      AND from_status='skipped'
      AND to_status='pending'
      AND actor_ref='trusted_temporal_compiler_retry';
  "
)" = 1

phase=protected_verification
capture_static "$static_after"
capture_isolation "$isolation_after"
qdrant_signature >"$qdrant_after"
cmp "$static_before" "$static_after"
cmp "$isolation_before" "$isolation_after"
cmp "$qdrant_before" "$qdrant_after"

phase=service_health
test "$(systemctl is-active brains.service)" = active
test "$(
  curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --max-time 30 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz
)" = 200

phase=timer_restore
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  test "$(systemctl is-enabled "$unit")" = "$enabled"
  test "$(systemctl is-active "$unit")" = "$active"
done <"$timer_state"

printf '%s\n' \
  'memory_v1_trusted_temporal_retry_production: PASS' \
  "production_head=$(git -C "$live" rev-parse HEAD)" \
  "backup=$backup_dir/memory.dump" \
  "rollback=$backup_dir/rollback.sql" \
  'retry_events=1 replay_writes=0 local_model_calls=1 external_model_calls=0' \
  'job_status=review_required attempts=3 helsing_death=1' \
  'claims=0 qdrant=0 prompt_influence=0 cross_owner_visible=0' \
  'timers_restored=true brains=active'
