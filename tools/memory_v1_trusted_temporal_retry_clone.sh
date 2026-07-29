#!/usr/bin/env bash
set -euo pipefail

repo=${REPO_ROOT:-/tmp/chat-memory-pet-temporal-integration-v2}
production=/opt/chat-memory
container=brains-postgres-1
source_db=memory
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
migration=$repo/ops/sql/20260729_memory_v1_trusted_temporal_retry.sql
canary=$repo/scripts/memory_v1_v5_local_inference_canary.py
api_key_file=/etc/memory-v1-local-inference/api-key
python_bin=/opt/chat-memory/venv/bin/python
clone_db=memory_trusted_temporal_retry_$$
artifact_dir=$(mktemp -d /tmp/memory-trusted-temporal-retry.XXXXXX)
timer_state=$artifact_dir/timers.tsv
protected_before=$artifact_dir/protected-before.tsv
protected_after=$artifact_dir/protected-after.tsv
qdrant_before=$artifact_dir/qdrant-before.sha
qdrant_after=$artifact_dir/qdrant-after.sha
canary_output=$artifact_dir/canary.json
canary_log=$artifact_dir/canary.log
timers_restored=0
stage=preflight

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
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
  if [[ "$rc" != 0 ]]; then
    printf 'memory_v1_trusted_temporal_retry_clone: FAIL stage=%s rc=%s\n' \
      "$stage" "$rc" >&2
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
  fi
  rm -rf "$artifact_dir"
  exit "$rc"
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

protected_snapshot() {
  local output=$1
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atq \
    -v ON_ERROR_STOP=1 >"$output" <<'SQL'
SELECT 'claim',count(*),coalesce(max(created_at)::text,'')
FROM memory.claim
UNION ALL
SELECT 'entity',count(*),coalesce(max(created_at)::text,'')
FROM memory.entity
UNION ALL
SELECT 'observation',count(*),coalesce(max(created_at)::text,'')
FROM memory.observation
UNION ALL
SELECT 'projection_outbox',count(*),coalesce(max(created_at)::text,'')
FROM memory.projection_outbox
UNION ALL
SELECT 'answer_binding',count(*),coalesce(max(created_at)::text,'')
FROM memory.final_answer_memory_binding_v1
ORDER BY 1;
SQL
}

test "$(id -u)" -eq 0
test -z "$(git -C "$production" status --porcelain)"
test -z "$(git -C "$repo" status --porcelain)"
test -r "$migration"
test -r "$canary"
test -r "$api_key_file"

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
  docker exec "$container" psql -U sage -d "$source_db" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE lease_token IS NOT NULL
      AND lease_expires_at > clock_timestamp();
  "
)" = 0

set -a
source "$production/.env"
set +a
test -n "${POSTGRES_DSN:-}"

stage=clone_create
docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"
docker exec -i "$container" psql -U sage -d "$clone_db" \
  -X -v ON_ERROR_STOP=1 <"$migration" >/dev/null

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" \
  "$python_bin" - <<'PY'
from urllib.parse import urlsplit, urlunsplit
import os

source = urlsplit(os.environ["SOURCE_DSN"])
if source.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("source DSN scheme is invalid")
if source.hostname not in {"127.0.0.1", "::1", "localhost"}:
    raise SystemExit("source DSN is not loopback")
print(urlunsplit((
    source.scheme,
    source.netloc,
    "/" + os.environ["CLONE_DB"],
    source.query,
    source.fragment,
)))
PY
)

stage=target_preflight
test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND job_id='$job'::uuid
      AND evidence_id='$evidence'::uuid
      AND evidence_content_sha256='$content_sha'
      AND status='skipped'
      AND attempts=2
      AND last_error=
          'local_inference_rejected: '||
          'trusted_source_time_asserted_by_provider'
      AND lease_token IS NULL
      AND lease_expires_at IS NULL;
  "
)" = 1
test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_packet_v5_local
    WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid;
  "
)" = 0

stage=cross_owner_rejection
if POSTGRES_DSN="$clone_dsn" psql "$clone_dsn" -X -q \
    -v ON_ERROR_STOP=1 >/dev/null 2>&1 <<SQL
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

protected_snapshot "$protected_before"
qdrant_signature >"$qdrant_before"

stage=reviewed_retry_apply
apply_result=$(
  POSTGRES_DSN="$clone_dsn" psql "$clone_dsn" -X -Atq \
    -v ON_ERROR_STOP=1 <<SQL | tail -1
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

replay_result=$(
  POSTGRES_DSN="$clone_dsn" psql "$clone_dsn" -X -Atq \
    -v ON_ERROR_STOP=1 <<SQL | tail -1
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

stage=private_canary
set +e
MEMORY_V1_V5_LOCAL_INFERENCE_APPLY=memory_v1_v5_local_inference_canary_apply_v1 \
MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(<"$api_key_file")" \
POSTGRES_DSN="$clone_dsn" \
PYTHONPATH="$repo" \
  "$python_bin" "$canary" \
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
  and .local_model_calls==1
  and .external_model_calls==0
  and .manual_review_required==true
  and .write_counts.claims==0
  and .write_counts.qdrant==0
  and .write_counts.prompt_influence==0
' "$canary_output" >/dev/null

stage=semantic_verification
docker exec "$container" psql -U sage -d "$clone_db" -X -Atq \
  -v ON_ERROR_STOP=1 -c "
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
assert temporal["relative_offset"] == {
    "direction": "past",
    "magnitude": 1.0,
    "unit": "year",
    "approximate": True,
    "anchor_source": "evidence_observed_at",
}
PY

test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
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
test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND job_id='$job'::uuid
      AND status='review_required'
      AND attempts=3
      AND last_error IS NULL;
  "
)" = 1

protected_snapshot "$protected_after"
cmp "$protected_before" "$protected_after"
qdrant_signature >"$qdrant_after"
cmp "$qdrant_before" "$qdrant_after"

restore_timers
stage=final
test "$(systemctl is-active brains.service)" = active
printf '%s\n' \
  'memory_v1_trusted_temporal_retry_clone: PASS' \
  'retry_events=1 replay_writes=0 local_model_calls=1 external_model_calls=0' \
  'job_status=review_required attempts=3 helsing_death=1' \
  'claims=0 qdrant=0 prompt_influence=0 cross_owner_visible=0'
