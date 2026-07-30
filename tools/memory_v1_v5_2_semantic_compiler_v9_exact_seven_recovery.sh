#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Resumes the exact-seven private extraction after the
# v9 schema/code deployment and selector enqueue have already succeeded.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for the private endpoint key' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_SEMANTIC_COMPILER_V9_RECOVERY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_SEMANTIC_COMPILER_V9_RECOVERY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
selector=20260730_v5_2_semantic_compiler_v9_reextract_v1
compiler_sha=738cc80f374e3c7e441fd03f964b77d01401422d286a9bc52205c6e060767ae6
manifest_sha=dab07b531eb987fba80b67cbc4c106a4d1fe3375add7cb9bb0de7842eaa6b91e
writer_sha=2b3182f59091a7d93697ae79ee6a8e1cc7541829f957b5a859c47cf28707d190
manifest=manifests/memory_v1_v5_2_semantic_compiler_v9_reextract_20260730.json
provider=scripts/memory_v1_relational_extraction_v5_local_provider.py
canary=tools/memory_v1_v5_local_inference_canary_apply.sh
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_semantic_compiler_v9_recovery.lock
required_live_ancestor=335d046dee3006a528aa40a020382076008ae1ff
expected_provider_sha=d30a59c35d671610dc06af12f0fa55b8b1520bb0c3ffdaef9041afe3949af134
expected_canary_sha=5b553819663f95406a82494611e0423be64f995833b4df302ee234dbd97b2cb3

target_jobs_sql="'bcab0f19-5f3c-54b3-97e1-deac045021f0'::uuid,
  'ad81be18-93b0-5907-aefa-9a03e00e79cf'::uuid,
  'fa71da24-d450-5447-8f4d-766c4d4ab446'::uuid,
  '93c0eec3-8968-5aa7-b5de-e81f27aac982'::uuid,
  '18525bf1-a1d6-579e-8252-d12296f64b6d'::uuid,
  '46b47a61-68d6-587c-8daf-f009e2de76f6'::uuid,
  '547a1438-e984-538c-8739-4a4dc53b283c'::uuid"
target_terminals_sql="'1c9280ec-e637-5378-8010-bbfab2b0d42a'::uuid,
  'b7e4ddcc-0af1-5bbd-9ba7-eefa0f519c85'::uuid,
  '1279f066-6e56-5c86-ab73-e27023121547'::uuid,
  '37b49e89-e6b5-569f-bea3-663f33532dd7'::uuid,
  '0e5fd9da-b8b6-55fa-95a9-2019fe1e49ad'::uuid,
  '670b0089-f011-5d64-8b14-a89ec6c98762'::uuid,
  '040dd3d9-15a7-5f5d-b525-bba19c1c5de1'::uuid"

timer_state=$(mktemp /tmp/memory-v9-recovery-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v9-recovery-tables.XXXXXX)
before_static=$(mktemp /tmp/memory-v9-recovery-static-before.XXXXXX)
after_static=$(mktemp /tmp/memory-v9-recovery-static-after.XXXXXX)
before_isolation=$(mktemp /tmp/memory-v9-recovery-isolation-before.XXXXXX)
after_isolation=$(mktemp /tmp/memory-v9-recovery-isolation-after.XXXXXX)
canary_dir=$(mktemp -d /tmp/memory-v9-recovery-canaries.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$before_static" "$after_static" \
  "$before_isolation" "$after_isolation"
timers_quiesced=0
phase=initialization
status_file=

scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 -c "$1" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    else
      systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  rc=$?
  trap - EXIT
  if [[ "$timers_quiesced" -eq 1 ]]; then
    restore_timers || rc=1
  fi
  rm -f "$timer_state" "$table_list" "$before_static" "$after_static" \
    "$before_isolation" "$after_isolation"
  rm -rf "$canary_dir"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$phase" "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$rc"
}
trap record_exit EXIT

capture_static_state() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
        ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value) rows")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$table_list"
}

capture_isolation_state() {
  local output=$1
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    WITH state(label,row_json) AS (
      SELECT 'other_jobs',to_jsonb(value)::text
      FROM memory.evidence_extraction_job AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'other_terminals',to_jsonb(value)::text
      FROM memory.evidence_intake_terminal AS value
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
      WHERE owner_user_id='$owner'::uuid AND job_id NOT IN ($target_jobs_sql)
      UNION ALL
      SELECT 'same_owner_other_terminals',to_jsonb(value)::text
      FROM memory.evidence_intake_terminal AS value
      WHERE owner_user_id='$owner'::uuid
        AND terminal_id NOT IN ($target_terminals_sql)
      UNION ALL
      SELECT 'same_owner_other_events',to_jsonb(value)::text
      FROM memory.evidence_extraction_event AS value
      WHERE owner_user_id='$owner'::uuid AND job_id NOT IN ($target_jobs_sql)
      UNION ALL
      SELECT 'same_owner_other_ledger',to_jsonb(value)::text
      FROM memory.v5_local_inference_event AS value
      WHERE owner_user_id='$owner'::uuid
        AND (job_id IS NULL OR job_id NOT IN ($target_jobs_sql))
      UNION ALL
      SELECT 'same_owner_other_packets',to_jsonb(value)::text
      FROM memory.evidence_extraction_packet_v5_local AS value
      WHERE owner_user_id='$owner'::uuid AND job_id NOT IN ($target_jobs_sql)
    ), labels(label) AS (VALUES
      ('other_jobs'),('other_terminals'),('other_events'),
      ('other_ledger'),('other_packets'),
      ('same_owner_other_jobs'),('same_owner_other_terminals'),
      ('same_owner_other_events'),('same_owner_other_ledger'),
      ('same_owner_other_packets')
    )
    SELECT label || E'\\t' || count(row_json)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(
        row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM labels LEFT JOIN state USING(label)
    GROUP BY label ORDER BY label
  " >"$output"
}

[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_live_ancestor" HEAD
[[ "$(sha256sum "$provider" | awk '{print $1}')" == "$expected_provider_sha" ]]
[[ "$(sha256sum "$canary" | awk '{print $1}')" == "$expected_canary_sha" ]]
[[ "$(jq -cS . "$manifest" | tr -d '\n' | sha256sum | awk '{print $1}')" \
  == "$manifest_sha" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8088/docs)" == 200 ]]
[[ "$(scalar "SELECT encode(public.digest(convert_to(pg_get_functiondef(
  'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
),'UTF8'),'sha256'),'hex')")" == "$writer_sha" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND selector_version='$selector'")" == 7 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$other'::uuid AND selector_version='$selector'")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND selector_version='$selector'
    AND status='pending' AND attempts=0
    AND lease_token IS NULL AND lease_expires_at IS NULL")" == 7 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id IN ($target_jobs_sql)")" == 0 ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_semantic_compiler_v9_recovery_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_semantic_compiler_v9_recovery_${run_tag}.json"

phase=inventory_timers
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
timer_count=$(wc -l <"$timer_state")
(( timer_count >= 15 && timer_count <= 40 ))

phase=quiesce_timers
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

phase=fresh_backup
partial="$snapshot_dir/.memory_pre_v9_recovery_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v9_recovery_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -U sage -d "$database" -X -At -F $'\t' \
  -c "SELECT table_schema,table_name
      FROM information_schema.tables
      WHERE table_type='BASE TABLE' AND table_schema='memory'
        AND table_name NOT IN (
          'evidence_extraction_job','evidence_intake_terminal',
          'evidence_extraction_event','v5_local_inference_event',
          'evidence_extraction_packet_v5_local'
        )
      ORDER BY table_schema,table_name" >"$table_list"
capture_static_state "$before_static"
capture_isolation_state "$before_isolation"
qdrant_before=$(qdrant_signature)
jobs_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')
terminals_before=$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')
events_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')
ledger_before=$(scalar 'SELECT count(*) FROM memory.v5_local_inference_event')
packets_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')

phase=private_extraction
index=0
while IFS= read -r item; do
  index=$((index+1))
  job=$(jq -r '.job_id' <<<"$item")
  evidence=$(jq -r '.evidence_id' <<<"$item")
  content=$(jq -r '.content_sha256' <<<"$item")
  run_id=$(jq -r '.run_id' <<<"$item")
  output="$canary_dir/$index.out"
  MEMORY_V1_PREDICATE_CONTRACT_PROFILE=v5_2 \
  MEMORY_V1_V5_LOCAL_INFERENCE_CANARY=authorized \
  MEMORY_V1_V5_2_LOCAL_INFERENCE_CANARY=authorized \
    bash "$canary" "$job" "$evidence" "$content" "$run_id" 0 4096 \
      >"$output"
  grep -qx 'outcome=accepted' "$output"
done < <(jq -cS '.items[]' "$manifest")
[[ "$index" == 7 ]]

phase=postflight
capture_static_state "$after_static"
capture_isolation_state "$after_isolation"
cmp -s "$before_static" "$after_static"
cmp -s "$before_isolation" "$after_isolation"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')" \
  == "$jobs_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')" \
  == "$terminals_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" \
  == "$((events_before+14))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_inference_event')" \
  == "$((ledger_before+14))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')" \
  == "$((packets_before+7))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND selector_version='$selector'
    AND status='review_required' AND attempts=1")" == 7 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id IN ($target_jobs_sql)
    AND policy_compiler_sha256='$compiler_sha'
    AND external_model_calls=0")" == 7 ]]
local_calls=$(scalar "SELECT coalesce(sum(local_model_calls),0)
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id IN ($target_jobs_sql)")
(( local_calls >= 0 && local_calls <= 1 ))
[[ "$(scalar "SELECT coalesce(sum(local_model_calls),0)
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND job_id IN ($target_jobs_sql)
    AND job_id<>'ad81be18-93b0-5907-aefa-9a03e00e79cf'::uuid")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid
    AND packet_id IN (
      SELECT packet_id FROM memory.evidence_extraction_packet_v5_local
      WHERE job_id IN ($target_jobs_sql)
    )")" == 0 ]]

phase=restore_timers
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"

phase=report
packet_summary=$(docker exec "$container" psql -U sage -d "$database" \
  -X -Atqc "
  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'job_id',job_id,
    'local_model_calls',local_model_calls,
    'manual_review_required',manual_review_required,
    'entities',jsonb_array_length(normalized_packet->'entity_mentions'),
    'observations',jsonb_array_length(normalized_packet->'observations'),
    'deferrals',jsonb_array_length(normalized_packet->'deferrals')
  ) ORDER BY job_id),'[]'::jsonb)
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id IN ($target_jobs_sql)")
jq -n \
  --arg contract_version memory_v1_v5_2_semantic_compiler_v9_recovery_v1 \
  --arg commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson timer_count "$timer_count" \
  --argjson local_model_calls "$local_calls" \
  --argjson packet_summary "$packet_summary" \
  '{
    contract_version:$contract_version,
    outcome:"pass",
    commit:$commit,
    manifest_sha256:$manifest_sha256,
    compiler_version:"memory_v1_semantic_policy_compiler_v9",
    bounded_recovery_writes:{
      extraction_events:14,local_inference_events:14,immutable_packets:7
    },
    local_model_calls:$local_model_calls,
    external_model_calls:0,
    packets:$packet_summary,
    selector_replay_previously_proved:true,
    packet_replay_proved:true,
    account_isolation_proved:true,
    non_target_memory_rows_unchanged:true,
    qdrant_unchanged:true,
    routing:0,staging:0,claims:0,projections:0,
    retrieval:0,prompt_influence:0,
    timers:{count:$timer_count,restored:true},
    backup:{path:$backup,sha256:$backup_sha256},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_durable_stance_promotion"
  }' >"$report"
chmod 0600 "$report"
report_sha=$(sha256sum "$report" | awk '{print $1}')
printf '%s  %s\n' "$report_sha" "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_2_semantic_compiler_v9_recovery: PASS\n'
printf 'report=%s\nreport_sha256=%s\nbackup=%s\nbackup_sha256=%s\n' \
  "$report" "$report_sha" "$backup" "$backup_sha"
