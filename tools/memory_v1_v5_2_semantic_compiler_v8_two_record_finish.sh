#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Enqueues and privately extracts the exact two reviewed
# replacement records, validates their immutable compiler-v8 packets, and
# stops before routing, staging, claims, projection, retrieval, or prompts.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for the private endpoint key' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_SEMANTIC_COMPILER_V8_FINISH:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_SEMANTIC_COMPILER_V8_FINISH=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-$repo_root/.env}"
set +a

container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
selector=20260725_v5_2_semantic_compiler_v8_reextract_v1
compiler_sha=f82e6f4339dfe4aada7e5c3edb71fde8125a819f33677b3f47b98f3726b60419
manifest_sha=98e36ec66a5136a5bea4c3438be11acda18ceda2865b393cdb58b6bb3def337f
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_semantic_compiler_v8_finish.lock
review_sql=tests/memory_v1_v5_2_semantic_compiler_v8_live_packets.sql
canary=tools/memory_v1_v5_local_inference_canary_apply.sh

care_operation=6d907502-d830-5467-9e61-fe846453383d
care_job=d0585d84-7386-5b5f-b7ff-ac97949883cc
care_terminal=25717c16-e87c-54ff-92da-d071770f6ab7
care_evidence=fea59e7e-30f5-4139-b634-97b291c88e14
care_content=895146b94431f7e0ec3292e757e30fc4c782af222bd39598e610654adf08ccec
care_prior_packet=a7f23e7b-89ff-5d23-bb75-79473be6f57d
care_prior_storage=430264a8f709288562505c0e50c97e2b379af59536ff5a65061352c3b925789e
care_run=39f6b695-2255-53ec-ae5b-c69ba8b2fa1e

profession_operation=87074bcf-7206-5032-8c25-f98c1b79f31c
profession_job=360dbb78-69e3-5a3d-ba9b-2512acca21b8
profession_terminal=059d58f1-eb02-559a-b30b-f55f51e1af7c
profession_evidence=dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5
profession_content=fb65fe592059bace88a4daea571ac1ba7df2b5aeb5136233dd01071cb6085182
profession_prior_packet=842c2fc5-d2ca-584d-a95c-98655e942732
profession_prior_storage=968611c8092391ffcdaa5ab493dbe7d1eb574c0a8e1748915c3ccb24c22bf029
profession_run=cc3cbe74-3e2f-5ce9-ad13-48e3f5309864

timer_state=$(mktemp /tmp/memory-v5-2-compiler-v8-finish-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v5-2-compiler-v8-finish-tables.XXXXXX)
before_static=$(mktemp /tmp/memory-v5-2-compiler-v8-finish-static-before.XXXXXX)
after_static=$(mktemp /tmp/memory-v5-2-compiler-v8-finish-static-after.XXXXXX)
before_isolation=$(mktemp /tmp/memory-v5-2-compiler-v8-finish-isolation-before.XXXXXX)
after_isolation=$(mktemp /tmp/memory-v5-2-compiler-v8-finish-isolation-after.XXXXXX)
apply_output=$(mktemp /tmp/memory-v5-2-compiler-v8-finish-apply.XXXXXX)
replay_output=$(mktemp /tmp/memory-v5-2-compiler-v8-finish-replay.XXXXXX)
care_output=$(mktemp /tmp/memory-v5-2-compiler-v8-finish-care.XXXXXX)
profession_output=$(mktemp /tmp/memory-v5-2-compiler-v8-finish-profession.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$before_static" "$after_static" \
  "$before_isolation" "$after_isolation" "$apply_output" "$replay_output" \
  "$care_output" "$profession_output"
timers_quiesced=0
phase=initialization
status_file=

scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 -c "$1" | tr -d '[:space:]'
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
    if [[ "$(systemctl is-active brains.service)" == active ]]; then
      restore_timers || rc=1
    else
      rc=1
    fi
  fi
  rm -f "$timer_state" "$table_list" "$before_static" "$after_static" \
    "$before_isolation" "$after_isolation" "$apply_output" "$replay_output" \
    "$care_output" "$profession_output"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$phase" "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$rc"
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
      WHERE owner_user_id='$owner'::uuid
        AND job_id NOT IN ('$care_job'::uuid,'$profession_job'::uuid)
      UNION ALL
      SELECT 'same_owner_other_terminals',to_jsonb(value)::text
      FROM memory.evidence_intake_terminal AS value
      WHERE owner_user_id='$owner'::uuid
        AND terminal_id NOT IN (
          '$care_terminal'::uuid,'$profession_terminal'::uuid
        )
      UNION ALL
      SELECT 'same_owner_other_events',to_jsonb(value)::text
      FROM memory.evidence_extraction_event AS value
      WHERE owner_user_id='$owner'::uuid
        AND job_id NOT IN ('$care_job'::uuid,'$profession_job'::uuid)
      UNION ALL
      SELECT 'same_owner_other_ledger',to_jsonb(value)::text
      FROM memory.v5_local_inference_event AS value
      WHERE owner_user_id='$owner'::uuid
        AND (
          job_id IS NULL
          OR job_id NOT IN ('$care_job'::uuid,'$profession_job'::uuid)
        )
      UNION ALL
      SELECT 'same_owner_other_packets',to_jsonb(value)::text
      FROM memory.evidence_extraction_packet_v5_local AS value
      WHERE owner_user_id='$owner'::uuid
        AND job_id NOT IN ('$care_job'::uuid,'$profession_job'::uuid)
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

[[ -f "$review_sql" && -x "$canary" ]]
[[ -z "$(git status --porcelain)" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8088/docs)" == 200 ]]
[[ "$(qdrant_signature)" == \
  b8c846a4d14b47e09773ef93ef0917e00bd1a21143e8ee4f15ddadacf4d42fb6 ]]
[[ "$(scalar "SELECT (
  to_regprocedure(
    'memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1(
      uuid,uuid,uuid,uuid,text,uuid,text,text,text
    )'
  ) IS NOT NULL
  AND position('memory_v1_semantic_policy_compiler_v8' IN pg_get_functiondef(
    'memory.persist_owner_v5_2_local_packet_v1(
      uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,
      jsonb,boolean,integer
    )'::regprocedure
  ))>0
)::integer")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND job_id IN ('$care_job'::uuid,'$profession_job'::uuid)")" == 0 ]]

reserved=$(scalar "SELECT count(*)
  FROM memory.v5_local_inference_event
  WHERE owner_user_id='$owner'::uuid
    AND action='reserved'
    AND created_at>=clock_timestamp()-interval '24 hours'")
[[ "$reserved" =~ ^[0-9]+$ ]]
if (( reserved > 10 )); then
  next_open=$(docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    SELECT min(created_at+interval '24 hours')
    FROM memory.v5_local_inference_event
    WHERE owner_user_id='$owner'::uuid
      AND action='reserved'
      AND created_at>=clock_timestamp()-interval '24 hours'")
  printf 'QUOTA_WAIT reserved=%s next_open=%s\n' "$reserved" "$next_open"
  exit 75
fi

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_semantic_compiler_v8_finish_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_semantic_compiler_v8_finish_${run_tag}.json"

phase=inventory_timers
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
timer_count=$(wc -l <"$timer_state")
(( timer_count >= 15 && timer_count <= 32 ))

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
partial="$snapshot_dir/.memory_pre_v5_2_semantic_compiler_v8_finish_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_semantic_compiler_v8_finish_${run_tag}.dump"
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
      WHERE table_type='BASE TABLE'
        AND table_schema IN ('memory','public')
        AND NOT (
          table_schema='memory' AND table_name IN (
            'evidence_extraction_job','evidence_intake_terminal',
            'evidence_extraction_event','v5_local_inference_event',
            'evidence_extraction_packet_v5_local'
          )
        )
        AND NOT (
          table_schema='public' AND table_name='telemetry_event'
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

phase=transactional_enqueue
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 -At -F $'\t' \
  -v owner="$owner" -v manifest="$manifest_sha" -v compiler="$compiler_sha" \
  >"$apply_output" <<SQL
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT * FROM memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1(
  '$care_operation','$care_job','$care_terminal','$care_evidence',
  '$care_content','$care_prior_packet','$care_prior_storage',
  :'manifest',:'compiler'
);
SELECT * FROM memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1(
  '$profession_operation','$profession_job','$profession_terminal',
  '$profession_evidence','$profession_content','$profession_prior_packet',
  '$profession_prior_storage',:'manifest',:'compiler'
);
COMMIT;
SQL
[[ "$(grep -c $'\tpending\tapplied$' "$apply_output")" == 2 ]]

phase=zero_write_replay
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 -At -F $'\t' \
  -v owner="$owner" -v manifest="$manifest_sha" -v compiler="$compiler_sha" \
  >"$replay_output" <<SQL
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT * FROM memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1(
  '$care_operation','$care_job','$care_terminal','$care_evidence',
  '$care_content','$care_prior_packet','$care_prior_storage',
  :'manifest',:'compiler'
);
SELECT * FROM memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1(
  '$profession_operation','$profession_job','$profession_terminal',
  '$profession_evidence','$profession_content','$profession_prior_packet',
  '$profession_prior_storage',:'manifest',:'compiler'
);
COMMIT;
SQL
[[ "$(grep -c $'\tpending\treplayed$' "$replay_output")" == 2 ]]

phase=caregiving_canary
MEMORY_V1_PREDICATE_CONTRACT_PROFILE=v5_2 \
MEMORY_V1_V5_LOCAL_INFERENCE_CANARY=authorized \
MEMORY_V1_V5_2_LOCAL_INFERENCE_CANARY=authorized \
  bash "$canary" "$care_job" "$care_evidence" "$care_content" \
    "$care_run" 0 4096 >"$care_output"
grep -qx 'outcome=accepted' "$care_output"

phase=former_profession_canary
MEMORY_V1_PREDICATE_CONTRACT_PROFILE=v5_2 \
MEMORY_V1_V5_LOCAL_INFERENCE_CANARY=authorized \
MEMORY_V1_V5_2_LOCAL_INFERENCE_CANARY=authorized \
  bash "$canary" "$profession_job" "$profession_evidence" \
    "$profession_content" "$profession_run" 0 4096 >"$profession_output"
grep -qx 'outcome=accepted' "$profession_output"

phase=semantic_review
docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 <"$review_sql" >/dev/null

phase=postflight
capture_static_state "$after_static"
capture_isolation_state "$after_isolation"
cmp -s "$before_static" "$after_static"
cmp -s "$before_isolation" "$after_isolation"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')" \
  == "$((jobs_before+2))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')" \
  == "$((terminals_before+2))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" \
  == "$((events_before+6))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_inference_event')" \
  == "$((ledger_before+4))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')" \
  == "$((packets_before+2))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND selector_version='$selector'
    AND status='review_required' AND attempts=1")" == 2 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$other'::uuid
    AND selector_version='$selector'")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND job_id IN ('$care_job'::uuid,'$profession_job'::uuid)
    AND policy_compiler_sha256='$compiler_sha'
    AND external_model_calls=0")" == 2 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid
    AND packet_id IN (
      SELECT packet_id FROM memory.evidence_extraction_packet_v5_local
      WHERE job_id IN ('$care_job'::uuid,'$profession_job'::uuid)
    )")" == 0 ]]

phase=restore_timers
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"

phase=report
care_report=$(awk -F= '$1=="report"{print $2}' "$care_output")
profession_report=$(awk -F= '$1=="report"{print $2}' "$profession_output")
care_report_sha=$(sha256sum "$care_report" | awk '{print $1}')
profession_report_sha=$(sha256sum "$profession_report" | awk '{print $1}')
jq -n \
  --arg contract_version memory_v1_v5_2_semantic_compiler_v8_finish_v1 \
  --arg commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  --arg care_report "$care_report" \
  --arg care_report_sha256 "$care_report_sha" \
  --arg profession_report "$profession_report" \
  --arg profession_report_sha256 "$profession_report_sha" \
  --argjson timer_count "$timer_count" \
  '{
    contract_version:$contract_version,
    outcome:"pass",
    commit:$commit,
    manifest_sha256:$manifest_sha256,
    bounded_writes:{
      jobs:2,terminals:2,extraction_events:6,
      local_inference_events:4,immutable_packets:2
    },
    private_local_model_calls:1,
    external_model_calls:0,
    semantic_review:{
      caregiving_bounded_spans:true,
      caregiving_compound_split:true,
      coordinated_former_roles:true,
      current_employment_not_invented:true,
      credential_misclassification_absent:true
    },
    zero_write_replay_proved:true,
    account_isolation_proved:true,
    non_target_rows_unchanged:true,
    qdrant_unchanged:true,
    claims:0,
    routing:0,
    staging:0,
    retrieval:0,
    prompt_influence:0,
    timers:{count:$timer_count,restored:true},
    canaries:{
      caregiving:{report:$care_report,sha256:$care_report_sha256},
      former_profession:{
        report:$profession_report,sha256:$profession_report_sha256
      }
    },
    backup:{path:$backup,sha256:$backup_sha256},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_packet_routing_or_staging"
  }' >"$report"
chmod 0600 "$report"
report_sha=$(sha256sum "$report" | awk '{print $1}')
printf '%s  %s\n' "$report_sha" "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_2_semantic_compiler_v8_finish: PASS\n'
printf 'report=%s\nreport_sha256=%s\nbackup=%s\nbackup_sha256=%s\n' \
  "$report" "$report_sha" "$backup" "$backup_sha"
