#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Replays the three context-overflow records against a
# disposable production clone with a 2,048-token output ceiling.

if [[ ${EUID} -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 2
fi
if [[ "${MEMORY_V1_V5_2_CONTEXT_BUDGET_CLONE_VERIFY:-}" != authorized ]]; then
  echo 'context budget clone verification authorization is required' >&2
  exit 2
fi

repo=$(git rev-parse --show-toplevel)
cd "$repo"
runtime_env=/opt/chat-memory/.env
container=brains-postgres-1
production=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
clone="memory_context_budget_${$}"
snapshot_dir=/home/ubuntu/brains/snapshots
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
backup="$snapshot_dir/memory_pre_context_budget_clone_${run_tag}.dump"
report="$snapshot_dir/memory_context_budget_clone_${run_tag}.json"
work=$(mktemp -d /tmp/memory-context-budget-clone.XXXXXX)
timer_state="$work/timers.tsv"
credential_dir="$work/credential"
mkdir -p "$credential_dir" "$snapshot_dir"
chmod 0700 "$work" "$credential_dir"
timers_quiesced=0
clone_created=0

job_ids=(
  c9c5bc9a-0707-4a64-ae23-4755e886d8c7
  716e679e-00a8-444f-9f67-7082f9f65719
  707128d8-aa3a-4634-a021-0973fbee3c1b
)
migration=ops/sql/20260727_memory_v1_v5_2_context_budget_recovery.sql

scalar() {
  local database=$1 query=$2
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "$query"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_database() {
  local database=$1 output=$2 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(scalar "$database" "
      SELECT count(*)::text || E'\\t' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(scalar "$database" "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name")
  chmod 0600 "$output"
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    else
      [[ "$enabled" == disabled ]]
      systemctl disable "$unit" >/dev/null 2>&1 || true
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      [[ "$active" == inactive ]]
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

cleanup() {
  rc=$?
  trap - EXIT
  restore_timers || rc=1
  if [[ "$clone_created" -eq 1 ]]; then
    docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
      >/dev/null 2>&1 || rc=1
  fi
  rm -rf "$work"
  exit "$rc"
}
trap cleanup EXIT

[[ -z "$(git status --short)" ]]
test -r "$runtime_env"
test -r /etc/memory-v1-local-inference/api-key
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_local_transport_context_test.py >/dev/null
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_local_inference_scheduler_test.py >/dev/null
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_local_inference_continuous_drain_contract_test.py \
  >/dev/null
systemd-analyze verify \
  ops/systemd/memory-v1-v5-local-inference-scheduler.service \
  ops/systemd/memory-v1-v5-local-inference-scheduler.timer
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]

while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$timer_state" ]]
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  related_service=${unit%.timer}.service
  for _attempt in $(seq 1 60); do
    systemctl is-active --quiet "$related_service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$related_service"
done <"$timer_state"

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
chmod 0600 "$backup"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

production_before="$work/production-before.tsv"
production_after="$work/production-after.tsv"
scheduler_output="$work/scheduler.json"
capture_database "$production" "$production_before"
qdrant_before=$(qdrant_signature)

docker exec "$container" createdb -U sage -T template0 "$clone"
clone_created=1
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --clean --if-exists <"$backup"
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

set -a
source "$runtime_env"
set +a
clone_dsn=$(
  python3 - "$POSTGRES_DSN" "$clone" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit

parts = urlsplit(sys.argv[1])
if parts.hostname not in {"127.0.0.1", "::1", "localhost"}:
    raise SystemExit("production DSN is not loopback")
print(urlunsplit((parts.scheme, parts.netloc, "/" + sys.argv[2], parts.query, "")))
PY
)

ids_sql=$(printf "'%s'::uuid," "${job_ids[@]}")
ids_sql=${ids_sql%,}
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND job_id IN ($ids_sql)
    AND status='skipped' AND attempts=1
    AND last_error='local_inference_rejected: local_transport_http_rejected'")" \
  -eq 3 ]]
docker exec "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 -P pager=off -c "
    UPDATE memory.evidence_extraction_job
    SET available_at=clock_timestamp()+interval '1 day'
    WHERE owner_user_id='$owner'::uuid
      AND status IN ('pending','error')
      AND job_id NOT IN ($ids_sql);" >/dev/null

apply_output="$work/recovery-apply.txt"
replay_output="$work/recovery-replay.txt"
psql "$clone_dsn" -X -Atq -v ON_ERROR_STOP=1 <<SQL >"$apply_output"
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT apply_outcome FROM memory.requeue_owner_v5_2_context_budget_failure_v1(
  '52ffb345-3384-4b81-99a9-b271d27245a1'::uuid,
  'c9c5bc9a-0707-4a64-ae23-4755e886d8c7'::uuid,
  '5a8adf167d46b95dac70fcf0220e0aedfbc696b65a0cc0817ae51d9f1036737c',
  'ca88037d-2e65-595b-b7b9-d56f597a5e5c'::uuid,
  '601569ab-c680-4019-8316-57f3e2a0d75f'::uuid,
  1,'local_transport_http_rejected',
  'private_gpu_context_budget_recovery_v1'
);
SELECT apply_outcome FROM memory.requeue_owner_v5_2_context_budget_failure_v1(
  '0d9eb08b-1130-408c-a8cb-1052fdf4d7ac'::uuid,
  '716e679e-00a8-444f-9f67-7082f9f65719'::uuid,
  '1458bbf1860c62fae9998d9e161f7830ed5b15c23a98c7b12e12100bdefce807',
  'fd9ebb9f-7424-5a43-9592-3294df5ad960'::uuid,
  '8be05c76-6faf-40c1-a687-df7aa4920eb3'::uuid,
  1,'local_transport_http_rejected',
  'private_gpu_context_budget_recovery_v1'
);
SELECT apply_outcome FROM memory.requeue_owner_v5_2_context_budget_failure_v1(
  'c12fda14-6907-4f3f-a7e2-96b313ad7961'::uuid,
  '707128d8-aa3a-4634-a021-0973fbee3c1b'::uuid,
  'c01b32f1619ae3af5bbaa590a11e93801856c326c6e61c44821958a2aba848b0',
  'aabc8dce-ab7b-54a4-8974-fe08f7d0b3bb'::uuid,
  'edc23f59-aeec-44ad-9a8b-4be64eb5eda6'::uuid,
  1,'local_transport_http_rejected',
  'private_gpu_context_budget_recovery_v1'
);
COMMIT;
SQL
[[ "$(grep -cx applied "$apply_output")" -eq 3 ]]

psql "$clone_dsn" -X -Atq -v ON_ERROR_STOP=1 <<SQL >"$replay_output"
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT apply_outcome FROM memory.requeue_owner_v5_2_context_budget_failure_v1(
  '52ffb345-3384-4b81-99a9-b271d27245a1'::uuid,
  'c9c5bc9a-0707-4a64-ae23-4755e886d8c7'::uuid,
  '5a8adf167d46b95dac70fcf0220e0aedfbc696b65a0cc0817ae51d9f1036737c',
  'ca88037d-2e65-595b-b7b9-d56f597a5e5c'::uuid,
  '601569ab-c680-4019-8316-57f3e2a0d75f'::uuid,
  1,'local_transport_http_rejected',
  'private_gpu_context_budget_recovery_v1'
);
SELECT apply_outcome FROM memory.requeue_owner_v5_2_context_budget_failure_v1(
  '0d9eb08b-1130-408c-a8cb-1052fdf4d7ac'::uuid,
  '716e679e-00a8-444f-9f67-7082f9f65719'::uuid,
  '1458bbf1860c62fae9998d9e161f7830ed5b15c23a98c7b12e12100bdefce807',
  'fd9ebb9f-7424-5a43-9592-3294df5ad960'::uuid,
  '8be05c76-6faf-40c1-a687-df7aa4920eb3'::uuid,
  1,'local_transport_http_rejected',
  'private_gpu_context_budget_recovery_v1'
);
SELECT apply_outcome FROM memory.requeue_owner_v5_2_context_budget_failure_v1(
  'c12fda14-6907-4f3f-a7e2-96b313ad7961'::uuid,
  '707128d8-aa3a-4634-a021-0973fbee3c1b'::uuid,
  'c01b32f1619ae3af5bbaa590a11e93801856c326c6e61c44821958a2aba848b0',
  'aabc8dce-ab7b-54a4-8974-fe08f7d0b3bb'::uuid,
  'edc23f59-aeec-44ad-9a8b-4be64eb5eda6'::uuid,
  1,'local_transport_http_rejected',
  'private_gpu_context_budget_recovery_v1'
);
COMMIT;
SQL
[[ "$(grep -cx replayed "$replay_output")" -eq 3 ]]

psql "$clone_dsn" -X -v ON_ERROR_STOP=1 >/dev/null <<SQL
BEGIN;
SELECT set_config('app.user_id','$other_owner',true);
DO \$block\$
BEGIN
  PERFORM * FROM memory.requeue_owner_v5_2_context_budget_failure_v1(
    '52ffb345-3384-4b81-99a9-b271d27245a1'::uuid,
    'c9c5bc9a-0707-4a64-ae23-4755e886d8c7'::uuid,
    '5a8adf167d46b95dac70fcf0220e0aedfbc696b65a0cc0817ae51d9f1036737c',
    'ca88037d-2e65-595b-b7b9-d56f597a5e5c'::uuid,
    '601569ab-c680-4019-8316-57f3e2a0d75f'::uuid,
    1,'local_transport_http_rejected',
    'private_gpu_context_budget_recovery_v1'
  );
  RAISE EXCEPTION 'cross-owner recovery unexpectedly succeeded';
EXCEPTION
  WHEN check_violation THEN NULL;
END
\$block\$;
ROLLBACK;
SQL
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND job_id IN ($ids_sql)
    AND status='pending' AND attempts=1")" -eq 3 ]]

install -o root -g root -m 0400 \
  /etc/memory-v1-local-inference/api-key "$credential_dir/local_api_key"
started_ns=$(date +%s%N)
POSTGRES_DSN="$clone_dsn" \
PYTHONPATH="$repo" \
CREDENTIALS_DIRECTORY="$credential_dir" \
MEMORY_V1_V5_LOCAL_SCHEDULER_APPLY=memory_v1_v5_local_inference_scheduler_apply_v1 \
  /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_local_inference_scheduler.py \
  --owner-user-id "$owner" \
  --contract-profile v5_2 \
  --max-jobs 3 \
  --max-runtime-seconds 1800 \
  --max-attempts 2 \
  --lease-seconds 900 \
  --timeout-seconds 600 \
  --max-output-tokens 2048 \
  --rolling-window-seconds 3600 \
  --max-reserved-jobs 100 \
  --failure-threshold 5 \
  --apply >"$scheduler_output"
finished_ns=$(date +%s%N)
chmod 0600 "$scheduler_output"

jq -e '
  .worker_version=="memory_v1_v5_local_inference_scheduler_v2" and
  .predicate_contract_profile=="v5_2" and
  .outcome=="max_jobs_reached" and
  .processed==3 and
  .outcome_counts.accepted==3 and
  (.outcome_counts.rejected // 0)==0 and
  .local_model_calls==3 and .external_model_calls==0 and
  .blocked_owner_count==0
' "$scheduler_output" >/dev/null
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND job_id IN ($ids_sql)
    AND status='review_required' AND attempts=2")" -eq 3 ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id IN ($ids_sql)")" -eq 3 ]]

capture_database "$production" "$production_after"
cmp -s "$production_before" "$production_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

elapsed_seconds=$(
  python3 - "$started_ns" "$finished_ns" <<'PY'
import sys
print(round((int(sys.argv[2]) - int(sys.argv[1])) / 1_000_000_000, 3))
PY
)

jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson elapsed_seconds "$elapsed_seconds" \
  '{
    contract_version:"memory_v1_v5_2_context_budget_clone_verify_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:$backup,
    records:3,
    max_output_tokens:2048,
    recovery_failure_threshold:5,
    processed:3,
    accepted:3,
    rejected:0,
    local_model_calls:3,
    external_model_calls:0,
    elapsed_seconds:$elapsed_seconds,
    checks:{
      exact_failed_records:true,
      controlled_requeue_applied:true,
      controlled_requeue_replay:true,
      cross_owner_rejected:true,
      context_budget_fit:true,
      packets_created:3,
      production_unchanged:true,
      qdrant_unchanged:true,
      private_gpu_only:true
    },
    qdrant_sha256:$qdrant_sha256
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

restore_timers
printf '%s\n' \
  'memory_v1_v5_2_context_budget_clone_verify: PASS' \
  "report=$report" \
  "backup=$backup" \
  'processed=3' \
  'accepted=3' \
  'rejected=0' \
  "elapsed_seconds=$elapsed_seconds" \
  'production_unchanged=true' \
  'qdrant_unchanged=true' \
  'timers_restored=true'
