#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the tested V5.2 context-budget compatibility,
# requeues exactly three bound historical failures, reruns only those records
# through private inference, then resumes the sequential continuous drain.

if [[ ${EUID} -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 2
fi
if [[ "${MEMORY_V1_V5_2_CONTEXT_BUDGET_RECOVERY_APPLY:-}" != authorized ]]; then
  echo 'context budget production recovery authorization is required' >&2
  exit 2
fi

repo=/opt/chat-memory
runtime_env=$repo/.env
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
service=memory-v1-v5-local-inference-scheduler.service
timer=memory-v1-v5-local-inference-scheduler.timer
migration=ops/sql/20260727_memory_v1_v5_2_context_budget_recovery.sql
apply_sql=ops/sql/20260727_memory_v1_v5_2_context_budget_recovery_apply.sql
snapshot_dir=/home/ubuntu/brains/snapshots
expected_head=${MEMORY_V1_V5_2_CONTEXT_BUDGET_EXPECTED_HEAD:-}
clone_report=${MEMORY_V1_V5_2_CONTEXT_BUDGET_CLONE_REPORT:-}
clone_report_sha=${MEMORY_V1_V5_2_CONTEXT_BUDGET_CLONE_REPORT_SHA256:-}
provided_timer_state=${MEMORY_V1_V5_2_CONTEXT_BUDGET_TIMER_STATE_FILE:-}

job_ids=(
  c9c5bc9a-0707-4a64-ae23-4755e886d8c7
  716e679e-00a8-444f-9f67-7082f9f65719
  707128d8-aa3a-4634-a021-0973fbee3c1b
)
evidence_ids=(
  6cfe2787-ffae-58b7-b8e3-2d30cefd99b5
  6f07a10c-2e03-55ef-90e4-7839da3b0376
  6fe94786-8c5c-5a79-a4a8-7d2c70df4edb
)
content_hashes=(
  5a8adf167d46b95dac70fcf0220e0aedfbc696b65a0cc0817ae51d9f1036737c
  1458bbf1860c62fae9998d9e161f7830ed5b15c23a98c7b12e12100bdefce807
  c01b32f1619ae3af5bbaa590a11e93801856c326c6e61c44821958a2aba848b0
)
run_ids=(
  4d6075cf-9dad-4a0b-a4f3-f8ee9f2d9697
  bb06d4bb-e3d9-4abb-9889-b44ebfb38112
  3e2a20b2-9c36-4fa9-9b8e-4196af24451f
)

work=$(mktemp -d /tmp/memory-context-budget-production.XXXXXX)
timer_state=$work/timers.tsv
unit_backup=$work/$service
unchanged_before=$work/unchanged-before.tsv
unchanged_after=$work/unchanged-after.tsv
scoped_before=$work/scoped-before.tsv
scoped_after=$work/scoped-after.tsv
credential_dir=$work/credential
mkdir -p "$credential_dir" "$snapshot_dir"
chmod 0700 "$work" "$credential_dir"
timers_quiesced=0
unit_installed=0
deployment_committed=0

scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "$1"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_unchanged_tables() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    case "$table" in
      evidence_extraction_job|evidence_extraction_event|\
      v5_local_inference_event|evidence_extraction_packet_v5_local)
        continue
        ;;
    esac
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(scalar "
      SELECT count(*)::text || E'\\t' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(scalar "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name")
  chmod 0600 "$output"
}

capture_changed_table_non_targets() {
  local output=$1 ids_sql
  ids_sql=$(printf "'%s'::uuid," "${job_ids[@]}")
  ids_sql=${ids_sql%,}
  : >"$output"
  scalar "
    SELECT 'jobs' || E'\\t' || count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(
        to_jsonb(j)::text,E'\\n' ORDER BY to_jsonb(j)::text),''),
        'UTF8'),'sha256'),'hex')
    FROM memory.evidence_extraction_job AS j
    WHERE NOT (j.owner_user_id='$owner'::uuid AND j.job_id IN ($ids_sql))
    UNION ALL
    SELECT 'events' || E'\\t' || count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(
        to_jsonb(e)::text,E'\\n' ORDER BY to_jsonb(e)::text),''),
        'UTF8'),'sha256'),'hex')
    FROM memory.evidence_extraction_event AS e
    WHERE NOT (e.owner_user_id='$owner'::uuid AND e.job_id IN ($ids_sql))
    UNION ALL
    SELECT 'local_ledger' || E'\\t' || count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(
        to_jsonb(l)::text,E'\\n' ORDER BY to_jsonb(l)::text),''),
        'UTF8'),'sha256'),'hex')
    FROM memory.v5_local_inference_event AS l
    WHERE NOT (l.owner_user_id='$owner'::uuid AND l.job_id IN ($ids_sql))
    UNION ALL
    SELECT 'local_packets' || E'\\t' || count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(
        to_jsonb(p)::text,E'\\n' ORDER BY to_jsonb(p)::text),''),
        'UTF8'),'sha256'),'hex')
    FROM memory.evidence_extraction_packet_v5_local AS p
    WHERE NOT (p.owner_user_id='$owner'::uuid AND p.job_id IN ($ids_sql))
    ORDER BY 1" >"$output"
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
  if [[ "$deployment_committed" -eq 0 && "$unit_installed" -eq 1 ]]; then
    install -o root -g root -m 0644 "$unit_backup" \
      "/etc/systemd/system/$service" || rc=1
    systemctl daemon-reload || rc=1
  fi
  restore_timers || rc=1
  rm -rf "$work"
  exit "$rc"
}
trap cleanup EXIT

cd "$repo"
[[ -n "$expected_head" && "$(git rev-parse HEAD)" == "$expected_head" ]]
[[ -z "$(git status --short)" ]]
test -r "$runtime_env"
test -r /etc/memory-v1-local-inference/api-key
[[ -f "$clone_report" && "$clone_report" == "$snapshot_dir/"* ]]
[[ "$clone_report_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$(sha256sum "$clone_report" | awk '{print $1}')" == "$clone_report_sha" ]]
jq -e '
  .contract_version=="memory_v1_v5_2_context_budget_clone_verify_v1" and
  .records==3 and .processed==3 and .accepted==3 and .rejected==0 and
  .local_model_calls==3 and .external_model_calls==0 and
  .checks.controlled_requeue_applied==true and
  .checks.controlled_requeue_replay==true and
  .checks.cross_owner_rejected==true and
  .checks.context_budget_fit==true and
  .checks.production_unchanged==true and
  .checks.qdrant_unchanged==true
' "$clone_report" >/dev/null

sha256sum -c <<'HASHES'
2ac3dedab39085f1e61890fa4786991f7c9bc1db31888a757cd8d0004c8b6f8c  ops/sql/20260727_memory_v1_v5_2_context_budget_recovery.sql
96cc0be1843904017903640bed55b645d7fddb62f95edf06444fae68dac8bbb5  ops/sql/20260727_memory_v1_v5_2_context_budget_recovery_apply.sql
b1a19d6db476cbeea5e6afcd53f34a145c007af01e2cb2fc2d9dccd8097b936f  scripts/memory_v1_relational_extraction_v5_local_provider.py
b0d0d727f3bcd0e0183e470aa2866c313bd951256ffc16f671bf91345e8da874  scripts/memory_v1_v5_local_inference_canary.py
d8c40697d656c738a68464060d7d05af52a07754358d45f227d9d08aa7e4702f  ops/systemd/memory-v1-v5-local-inference-scheduler.service
15918b7afbdb9dc0a44328d7f4562e55d52f5d594680e798f489e534de3d38d8  scripts/memory_v1_v5_local_inference_continuous_drain_contract_test.py
HASHES
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_local_transport_context_test.py >/dev/null
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  scripts/memory_v1_v5_local_inference_continuous_drain_contract_test.py \
  >/dev/null
systemd-analyze verify ops/systemd/$service
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
curl --fail --silent --show-error --max-time 10 \
  http://127.0.0.1:18080/health >/dev/null

if [[ -n "$provided_timer_state" ]]; then
  [[ "$provided_timer_state" == "$snapshot_dir/"* ]]
  test -r "$provided_timer_state"
  awk 'NF==3 {print $1 "\t" $2 "\t" $3}' \
    "$provided_timer_state" >"$timer_state"
else
  while IFS= read -r unit; do
    printf '%s\t%s\t%s\n' "$unit" \
      "$(systemctl is-enabled "$unit")" \
      "$(systemctl is-active "$unit")" >>"$timer_state"
  done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u)
fi
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

run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
backup="$snapshot_dir/memory_pre_context_budget_recovery_${run_tag}.dump"
report="$snapshot_dir/memory_context_budget_recovery_${run_tag}.json"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
chmod 0600 "$backup"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

capture_unchanged_tables "$unchanged_before"
capture_changed_table_non_targets "$scoped_before"
qdrant_before=$(qdrant_signature)

ids_sql=$(printf "'%s'::uuid," "${job_ids[@]}")
ids_sql=${ids_sql%,}
exact_ready=$(scalar "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND job_id IN ($ids_sql)
    AND (
      (status='skipped' AND attempts=1 AND
       last_error='local_inference_rejected: local_transport_http_rejected')
      OR (status='pending' AND attempts=1 AND last_error IS NULL)
      OR (status='review_required' AND attempts=2 AND last_error IS NULL)
    )")
[[ "$exact_ready" -eq 3 ]]
existing_packets=$(scalar "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id IN ($ids_sql)")
[[ "$existing_packets" -ge 0 && "$existing_packets" -le 3 ]]

cp "/etc/systemd/system/$service" "$unit_backup"
chmod 0600 "$unit_backup"
install -o root -g root -m 0644 "ops/systemd/$service" \
  "/etc/systemd/system/$service"
systemctl daemon-reload
unit_installed=1
cmp -s "ops/systemd/$service" "/etc/systemd/system/$service"

docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

set -a
source "$runtime_env"
set +a
apply_output=$work/recovery-apply.txt
replay_output=$work/recovery-replay.txt
psql "$POSTGRES_DSN" -X -Atq -v ON_ERROR_STOP=1 \
  <"$apply_sql" >"$apply_output"
[[ "$(grep -Ec '^(applied|replayed)$' "$apply_output")" -eq 3 ]]
psql "$POSTGRES_DSN" -X -Atq -v ON_ERROR_STOP=1 \
  <"$apply_sql" >"$replay_output"
[[ "$(grep -cx replayed "$replay_output")" -eq 3 ]]

install -o root -g root -m 0400 \
  /etc/memory-v1-local-inference/api-key "$credential_dir/local_api_key"
calls_made=0
for index in 0 1 2; do
  current_state=$(scalar "
    SELECT status||':'||attempts::text FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND job_id='${job_ids[$index]}'::uuid")
  if [[ "$current_state" == review_required:2 ]]; then
    [[ "$(scalar "
      SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
      WHERE owner_user_id='$owner'::uuid
        AND job_id='${job_ids[$index]}'::uuid")" -eq 1 ]]
    continue
  fi
  [[ "$current_state" == pending:1 ]]
  output="$work/canary-$index.json"
  POSTGRES_DSN="$POSTGRES_DSN" \
  PYTHONPATH="$repo" \
  CREDENTIALS_DIRECTORY="$credential_dir" \
  MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(<"$credential_dir/local_api_key")" \
  MEMORY_V1_V5_LOCAL_INFERENCE_APPLY=memory_v1_v5_local_inference_canary_apply_v1 \
    /opt/chat-memory/venv/bin/python \
    scripts/memory_v1_v5_local_inference_canary.py \
    --owner-user-id "$owner" \
    --evidence-id "${evidence_ids[$index]}" \
    --expected-job-id "${job_ids[$index]}" \
    --expected-content-sha256 "${content_hashes[$index]}" \
    --selector-version 20260717_v2 \
    --contract-profile v5_2 \
    --run-id "${run_ids[$index]}" \
    --worker-id context-budget-recovery-v1 \
    --lease-seconds 900 \
    --max-attempts 2 \
    --timeout-seconds 600 \
    --max-output-tokens 2048 \
    --rolling-window-seconds 3600 \
    --max-reserved-jobs 100 \
    --failure-threshold 5 \
    --apply >"$output"
  chmod 0600 "$output"
  jq -e '
    .contract_version=="memory_v1_v5_local_inference_canary_v1" and
    .predicate_contract_profile=="v5_2" and
    .outcome=="accepted" and
    .local_model_calls==1 and .external_model_calls==0 and
    .write_counts.claims==0 and .write_counts.qdrant==0 and
    .write_counts.prompt_influence==0
  ' "$output" >/dev/null
  calls_made=$((calls_made+1))
done
[[ "$calls_made" -eq $((3-existing_packets)) ]]

[[ "$(scalar "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND job_id IN ($ids_sql)
    AND status='review_required' AND attempts=2")" -eq 3 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id IN ($ids_sql)")" -eq 3 ]]

capture_unchanged_tables "$unchanged_after"
capture_changed_table_non_targets "$scoped_after"
cmp -s "$unchanged_before" "$unchanged_after"
cmp -s "$scoped_before" "$scoped_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

restore_timers
systemctl start "$timer"
systemctl start --no-block "$service"
[[ "$(systemctl is-enabled "$timer")" == enabled ]]
[[ "$(systemctl is-active "$timer")" == active ]]

jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg clone_report "$clone_report" \
  --arg clone_report_sha256 "$clone_report_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson local_model_calls "$calls_made" \
  '{
    contract_version:"memory_v1_v5_2_context_budget_recovery_apply_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    clone_verification:{path:$clone_report,sha256:$clone_report_sha256},
    recovered_jobs:3,
    accepted_packets:3,
    local_model_calls:$local_model_calls,
    external_model_calls:0,
    checks:{
      hash_locked:true,
      controlled_requeue_applied:true,
      controlled_requeue_replay:true,
      exact_jobs_accepted:true,
      account_isolation:true,
      unchanged_non_target_records:true,
      qdrant_unchanged:true,
      claims_unchanged:true,
      prompt_influence_zero:true,
      timers_restored:true,
      continuous_drain_resumed:true
    },
    qdrant_sha256:$qdrant_sha256
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

deployment_committed=1
unit_installed=0
printf '%s\n' \
  'memory_v1_v5_2_context_budget_recovery_apply: PASS' \
  "report=$report" \
  "backup=$backup" \
  'recovered_jobs=3' \
  'accepted_packets=3' \
  'qdrant_unchanged=true' \
  'account_isolation=true' \
  'continuous_drain_resumed=true'
