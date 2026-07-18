#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Runs one exact owner-scoped canary through the private
# loopback endpoint and proves bounded writes, replay, account isolation, and
# unchanged Qdrant/non-target state. Must run as root so the endpoint key is
# never granted to the interactive account.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for the root-only endpoint key' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_LOCAL_INFERENCE_CANARY:-}" != "authorized" ]]; then
  echo 'MEMORY_V1_V5_LOCAL_INFERENCE_CANARY=authorized is required' >&2
  exit 1
fi
if [[ $# -lt 4 || $# -gt 5 ]]; then
  echo 'usage: canary_apply.sh JOB_ID EVIDENCE_ID CONTENT_SHA256 RUN_ID [PRIOR_ATTEMPTS]' >&2
  exit 2
fi

target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
target_job=$1
target_evidence=$2
target_content_sha=$3
run_id=$4
prior_attempts=${5:-0}
uuid_re='^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
sha_re='^[0-9a-f]{64}$'
[[ "$target_job" =~ $uuid_re ]]
[[ "$target_evidence" =~ $uuid_re ]]
[[ "$run_id" =~ $uuid_re ]]
[[ "$target_content_sha" =~ $sha_re ]]
[[ "$prior_attempts" =~ ^[0-9]+$ ]]
[[ "$prior_attempts" -ge 0 && "$prior_attempts" -le 19 ]]
max_attempts=$((prior_attempts+1))

repo_root=$(git rev-parse --show-toplevel)
canary=scripts/memory_v1_v5_local_inference_canary.py
provider=scripts/memory_v1_relational_extraction_v5_local_provider.py
expected_canary_sha=50ecc1bf6bc2a57cace736606f13afab60595ded52f21eb2e0b22203bbe78bc8
expected_provider_sha=5d0bd5b146986bf85a3dbf51e48234d5402c9fd4f917c6fff24dab500d48c484
api_key_file=/etc/memory-v1-local-inference/api-key
env_file=/opt/chat-memory/.env
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_inference_canary.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_state=$(mktemp /tmp/memory-v1-v5-local-canary-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-local-canary-tables.XXXXXX)

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

restore_timers() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

record_exit() {
  exit_code=$?
  restore_timers || exit_code=1
  rm -f "$unit_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_static_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

capture_isolation_state() {
  local output=$1
  psql_scalar "
    WITH state(label,row_json) AS (
      SELECT 'other_owner_jobs',to_jsonb(value)::text
      FROM memory.evidence_extraction_job AS value
      WHERE owner_user_id<>'$target_owner'::uuid
      UNION ALL
      SELECT 'other_owner_events',to_jsonb(value)::text
      FROM memory.evidence_extraction_event AS value
      WHERE owner_user_id<>'$target_owner'::uuid
      UNION ALL
      SELECT 'other_owner_local_ledger',to_jsonb(value)::text
      FROM memory.v5_local_inference_event AS value
      WHERE owner_user_id<>'$target_owner'::uuid
      UNION ALL
      SELECT 'other_owner_local_packets',to_jsonb(value)::text
      FROM memory.evidence_extraction_packet_v5_local AS value
      WHERE owner_user_id<>'$target_owner'::uuid
      UNION ALL
      SELECT 'same_owner_other_jobs',to_jsonb(value)::text
      FROM memory.evidence_extraction_job AS value
      WHERE owner_user_id='$target_owner'::uuid
        AND job_id<>'$target_job'::uuid
      UNION ALL
      SELECT 'same_owner_other_events',to_jsonb(value)::text
      FROM memory.evidence_extraction_event AS value
      WHERE owner_user_id='$target_owner'::uuid
        AND job_id<>'$target_job'::uuid
      UNION ALL
      SELECT 'same_owner_other_local_ledger',to_jsonb(value)::text
      FROM memory.v5_local_inference_event AS value
      WHERE owner_user_id='$target_owner'::uuid
        AND job_id IS DISTINCT FROM '$target_job'::uuid
      UNION ALL
      SELECT 'same_owner_other_local_packets',to_jsonb(value)::text
      FROM memory.evidence_extraction_packet_v5_local AS value
      WHERE owner_user_id='$target_owner'::uuid
        AND job_id<>'$target_job'::uuid
    ), labels(label) AS (VALUES
      ('other_owner_jobs'),('other_owner_events'),
      ('other_owner_local_ledger'),('other_owner_local_packets'),
      ('same_owner_other_jobs'),('same_owner_other_events'),
      ('same_owner_other_local_ledger'),('same_owner_other_local_packets')
    )
    SELECT label || E'\\t' || count(row_json)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(
        row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM labels LEFT JOIN state USING(label)
    GROUP BY label ORDER BY label
  " >"$output"
  chmod 0600 "$output"
}

for file in "$repo_root/$canary" "$repo_root/$provider" "$api_key_file" \
  "$env_file"; do
  [[ -f "$file" ]]
done
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(sha256sum "$repo_root/$canary" | awk '{print $1}')" == "$expected_canary_sha" ]]
[[ "$(sha256sum "$repo_root/$provider" | awk '{print $1}')" == "$expected_provider_sha" ]]
[[ "$(stat -c %a "$api_key_file")" == 600 ]]
[[ "$(stat -c %U:%G "$api_key_file")" == root:root ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_local_canary_${run_tag}.status"
before_static="$snapshot_dir/memory_v1_v5_local_canary_static_before_${run_tag}.tsv"
after_static="$snapshot_dir/memory_v1_v5_local_canary_static_after_${run_tag}.tsv"
before_isolation="$snapshot_dir/memory_v1_v5_local_canary_isolation_before_${run_tag}.tsv"
after_isolation="$snapshot_dir/memory_v1_v5_local_canary_isolation_after_${run_tag}.tsv"
canary_output="$snapshot_dir/memory_v1_v5_local_canary_output_${run_tag}.json"
canary_log="$snapshot_dir/memory_v1_v5_local_canary_${run_tag}.log"

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regclass('memory.v5_local_inference_event') IS NOT NULL
  AND to_regclass('memory.evidence_extraction_packet_v5_local') IS NOT NULL
)::integer")" == 1 ]]
resume_mode=$(psql_scalar "SELECT CASE WHEN EXISTS (
  SELECT 1
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence AS evidence USING(owner_user_id,evidence_id)
  JOIN memory.v5_local_inference_event AS event
    ON event.owner_user_id=job.owner_user_id AND event.job_id=job.job_id
  WHERE job.owner_user_id='$target_owner'::uuid
    AND job.job_id='$target_job'::uuid
    AND job.evidence_id='$target_evidence'::uuid
    AND job.evidence_content_sha256='$target_content_sha'
    AND job.status='processing' AND job.route='relational_extraction'
    AND job.attempts=$max_attempts AND job.lease_expires_at>clock_timestamp()
    AND evidence.status='active' AND event.action='reserved'
    AND event.run_id='$run_id'::uuid
) THEN 1 ELSE 0 END")
if [[ "$resume_mode" == 0 ]]; then
  [[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence AS evidence USING(owner_user_id,evidence_id)
  WHERE job.owner_user_id='$target_owner'::uuid
    AND job.job_id='$target_job'::uuid
    AND job.evidence_id='$target_evidence'::uuid
    AND job.evidence_content_sha256='$target_content_sha'
    AND job.status='pending' AND job.route='relational_extraction'
    AND job.attempts=$prior_attempts AND evidence.status='active'")" == 1 ]]
  [[ "$(psql_scalar "SELECT (
    count(*) FILTER (WHERE run_id='$run_id'::uuid) = 0
    AND count(*) = (2 * $prior_attempts)
    AND count(*) FILTER (WHERE action='reserved') = $prior_attempts
    AND count(*) FILTER (
      WHERE action='completed'
        AND outcome='rejected'
        AND rejection_code IN (
          'local_transport_timeout',
          'local_transport_unavailable',
          'local_transport_rate_limited',
          'local_transport_server_error'
        )
    ) = $prior_attempts
  )::integer
  FROM memory.v5_local_inference_event
  WHERE owner_user_id='$target_owner'::uuid
    AND job_id='$target_job'::uuid")" == 1 ]]
fi
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$target_owner'::uuid AND job_id='$target_job'::uuid")" == 0 ]]

: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ -s "$unit_state" ]]
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$unit_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"

phase=baseline
psql_scalar "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name NOT IN (
      'evidence_extraction_job','evidence_extraction_event',
      'v5_local_inference_event','evidence_extraction_packet_v5_local'
    )
  ORDER BY table_name
" >"$table_list"
capture_static_state "$before_static"
capture_isolation_state "$before_isolation"
qdrant_before=$(qdrant_signature)
target_events_before=$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$target_owner'::uuid AND job_id='$target_job'::uuid")
local_events_before=$(psql_scalar "SELECT count(*) FROM memory.v5_local_inference_event
  WHERE owner_user_id='$target_owner'::uuid AND job_id='$target_job'::uuid")
packets_before=$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$target_owner'::uuid AND job_id='$target_job'::uuid")

phase=private_local_canary
set -a
source "$env_file"
set +a
set +e
MEMORY_V1_V5_LOCAL_INFERENCE_APPLY=memory_v1_v5_local_inference_canary_apply_v1 \
MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(<"$api_key_file")" \
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$canary" \
    --owner-user-id "$target_owner" \
    --evidence-id "$target_evidence" \
    --expected-job-id "$target_job" \
    --expected-content-sha256 "$target_content_sha" \
    --max-attempts "$max_attempts" \
    --run-id "$run_id" --apply >"$canary_output" 2>"$canary_log"
canary_rc=$?
set -e
chmod 0600 "$canary_output" "$canary_log"
[[ -s "$canary_output" ]]
outcome=$(jq -r '.outcome' "$canary_output")
[[ "$outcome" == accepted || "$outcome" == rejected ]]
if [[ "$outcome" == accepted ]]; then
  [[ "$canary_rc" -eq 0 ]]
else
  [[ "$canary_rc" -eq 1 ]]
fi
jq -e '
  .external_model_calls==0
  and (.local_model_calls>=0 and .local_model_calls<=1)
  and .zero_write_replay_proved==true
  and .write_counts.claims==0
  and .write_counts.qdrant==0
  and .write_counts.prompt_influence==0
' "$canary_output" >/dev/null

phase=postflight
capture_static_state "$after_static"
capture_isolation_state "$after_isolation"
cmp -s "$before_static" "$after_static"
cmp -s "$before_isolation" "$after_isolation"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
target_events_after=$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$target_owner'::uuid AND job_id='$target_job'::uuid")
local_events_after=$(psql_scalar "SELECT count(*) FROM memory.v5_local_inference_event
  WHERE owner_user_id='$target_owner'::uuid AND job_id='$target_job'::uuid")
packets_after=$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$target_owner'::uuid AND job_id='$target_job'::uuid")
expected_target_event_delta=2
expected_local_event_delta=2
if [[ "$resume_mode" == 1 ]]; then
  expected_target_event_delta=1
  expected_local_event_delta=1
fi
[[ $((target_events_after-target_events_before)) -eq "$expected_target_event_delta" ]]
[[ $((local_events_after-local_events_before)) -eq "$expected_local_event_delta" ]]
if [[ "$outcome" == accepted ]]; then
  [[ $((packets_after-packets_before)) -eq 1 ]]
  expected_status=review_required
else
  [[ $((packets_after-packets_before)) -eq 0 ]]
  expected_status=$(psql_scalar "SELECT status::text FROM memory.evidence_extraction_job
    WHERE owner_user_id='$target_owner'::uuid AND job_id='$target_job'::uuid")
  [[ "$expected_status" != processing ]]
fi
[[ "$(psql_scalar "SELECT status::text FROM memory.evidence_extraction_job
  WHERE owner_user_id='$target_owner'::uuid AND job_id='$target_job'::uuid")" == "$expected_status" ]]

phase=rollback_only_cross_owner_probe
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U brains_app -d "$database" >/dev/null <<SQL
BEGIN;
SELECT set_config('app.user_id','00000000-0000-4000-8000-000000000123',true);
DO \$probe\$
BEGIN
  BEGIN
    PERFORM * FROM memory.claim_owner_v5_local_inference_job_v1(
      '00000000-0000-4000-8000-000000000124',
      '00000000-0000-4000-8000-000000000125',
      '$target_job'::uuid,'$target_content_sha','relational_extraction',
      'cross-owner-rollback-probe',60,1,'local_llama_cpp','v1',
      repeat('1',64),repeat('2',64),repeat('3',64),repeat('4',64),3600,1,1
    );
    IF FOUND THEN
      RAISE EXCEPTION 'cross-owner local claim unexpectedly returned a row'
        USING ERRCODE='42501';
    END IF;
  EXCEPTION WHEN check_violation THEN NULL;
  END;
END
\$probe\$;
ROLLBACK;
SQL

restore_timers

phase=report
report="$snapshot_dir/memory_v1_v5_local_canary_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg owner_sha256 "$(printf %s "$target_owner" | sha256sum | awk '{print $1}')" \
  --arg job_sha256 "$(printf %s "$target_job" | sha256sum | awk '{print $1}')" \
  --arg evidence_sha256 "$(printf %s "$target_evidence" | sha256sum | awk '{print $1}')" \
  --arg content_sha256 "$target_content_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  --arg before_static "$before_static" --arg after_static "$after_static" \
  --arg before_isolation "$before_isolation" --arg after_isolation "$after_isolation" \
  --arg log "$canary_log" --slurpfile canary "$canary_output" \
  '{
    contract_version:"memory_v1_v5_local_canary_apply_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    target:{owner_user_id_sha256:$owner_sha256,job_id_sha256:$job_sha256,
      evidence_id_sha256:$evidence_sha256,evidence_content_sha256:$content_sha256},
    canary:$canary[0],
    evidence:{static_before:$before_static,static_after:$after_static,
      isolation_before:$before_isolation,isolation_after:$after_isolation,
      log:$log,qdrant_sha256:$qdrant_sha256},
    checks:{private_local_endpoint:true,external_model_calls:0,
      target_scoped_writes_only:true,other_owners_unchanged:true,
      same_owner_non_target_rows_unchanged:true,zero_write_replay:true,
      cross_owner_claim_rejected:true,qdrant_unchanged:true,
      claim_promotion:false,retrieval_activation:false,prompt_influence:false,
      timers_restored:true},
    hard_stop:"before_promotion_or_live_retrieval"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_local_inference_canary_apply: PASS\n'
printf 'outcome=%s\nreport=%s\n' "$outcome" "$report"
