#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the generic reviewed-observation admission
# bridge, admits exactly three reviewed observations, and records exactly one
# private entailment assessment per observation. It creates no claims or
# Qdrant/prompt influence.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for backups and private inference' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_REVIEWED_OBSERVATION_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_REVIEWED_OBSERVATION_PRODUCTION=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

required_ancestor=23a94ed25ba44862d96cd95f0de9ff400fe3cf83
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
container=brains-postgres-1
database=memory
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_reviewed_observation.lock
migration=ops/sql/20260729_memory_v1_v5_2_reviewed_observation_stage.sql
rollback=ops/sql/20260729_memory_v1_v5_2_reviewed_observation_stage_rollback.sql
apply_sql=ops/sql/20260729_memory_v1_v5_2_reviewed_observation_stage_three_apply.sql
clone_test=tools/memory_v1_v5_2_reviewed_observation_stage_clone.sh
dispatcher=scripts/memory_v1_evidence_intake_dispatcher.py
dispatcher_test=tests/test_memory_v1_evidence_intake_dispatcher.py
dispatcher_clone=tools/memory_v1_evidence_intake_dispatcher_clone.sh

observations=(
  917ab793-6f03-4af4-847b-c87f5632fa91
  c0194481-bed5-438f-9407-07e398f14e50
  14e21c6b-1728-439b-9613-7d9b933d33b8
)
admissions=(
  35febb7e-0993-5ddf-b9b0-71a22d1b8501
  28f7e091-252e-5357-b64f-144f6445e2e8
  daa5a659-ba7c-5841-adb2-6aaa7c0481ae
)

declare -A expected_sha256=(
  ["$migration"]="5a9546c56738d58e798bce4fada5dfd63ddf2773b6beacd50534c41684b77547"
  ["$rollback"]="1ab5a24cdf2a99df65f9ec2d74a072eff6346770329d81af25c106b5e4774487"
  ["$apply_sql"]="2e956dffe1558b2e3c133f9ed9418b715e79df4fc866b8224eef659251d9d613"
  ["$clone_test"]="b2393ed3b4f527d33aa7ab29b0c427a1293ebc771c2cca132ca9454399c45109"
  ["$dispatcher"]="95ee5139581fbe0008ebf529d485e7efb703f02acecd2f56620520a3d9f15332"
  ["$dispatcher_test"]="c7d50c18eff6b8a33a8ef355cb3044f598e71b05a603628ff83a22b31fff9bcb"
  ["$dispatcher_clone"]="c26defb5ac7ba6d24b256fd5c7c1f820eec7b3614bd4e1d81957a78a68af9d83"
)

timer_state=$(mktemp /tmp/memory-v5-2-reviewed-observation-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v5-2-reviewed-observation-tables.XXXXXX)
target_before=$(mktemp /tmp/memory-v5-2-reviewed-observation-target-before.XXXXXX)
target_after=$(mktemp /tmp/memory-v5-2-reviewed-observation-target-after.XXXXXX)
other_before=$(mktemp /tmp/memory-v5-2-reviewed-observation-other-before.XXXXXX)
other_after=$(mktemp /tmp/memory-v5-2-reviewed-observation-other-after.XXXXXX)
clone_output=$(mktemp /tmp/memory-v5-2-reviewed-observation-clone.XXXXXX)
work=$(mktemp -d /tmp/memory-v5-2-reviewed-observation-work.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$target_before" "$target_after" \
  "$other_before" "$other_after" "$clone_output"

phase=initialization
timers_quiesced=0
migration_installed=0
writes_committed=0
status_file=
report_file=

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database" "$@"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

other_intended_signature() {
  scalar "
    WITH rows(label,row_json) AS (
      SELECT 'assessment',to_jsonb(value)::text
      FROM memory.v5_local_entailment_assessment AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'entailment',to_jsonb(value)::text
      FROM memory.observation_entailment_v5 AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'request',to_jsonb(value)::text
      FROM memory.relational_operation_request AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'inference_event',to_jsonb(value)::text
      FROM memory.v5_local_inference_event AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'inference_outcome',to_jsonb(value)::text
      FROM memory.v5_local_inference_outcome_event AS value
      WHERE owner_user_id<>'$owner'::uuid
    )
    SELECT encode(public.digest(convert_to(
      coalesce(string_agg(label||E'\\t'||row_json,E'\\n'
        ORDER BY label,row_json),''),
      'UTF8'),'sha256'),'hex')
    FROM rows
  "
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    case "$enabled" in
      enabled) systemctl enable "$unit" >/dev/null ;;
      disabled) systemctl disable "$unit" >/dev/null ;;
      *) return 1 ;;
    esac
    case "$active" in
      active) systemctl start "$unit" ;;
      inactive) systemctl stop "$unit" ;;
      *) return 1 ;;
    esac
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  code=$?
  trap - EXIT
  if [[ "$migration_installed" -eq 1 && "$writes_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || code=1
  fi
  restore_timers || code=1
  rm -f "$timer_state" "$table_list" "$target_before" "$target_after" \
    "$other_before" "$other_after" "$clone_output"
  rm -rf "$work"
  if [[ -n "$status_file" ]]; then
    {
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$code"
      printf 'writes_committed=%s\n' "$writes_committed"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
}
trap record_exit EXIT

capture_partition() {
  local partition=$1 output=$2 table state predicate
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    if [[ "$partition" == target ]]; then
      predicate="owner_user_id='$owner'::uuid"
    else
      predicate="owner_user_id<>'$owner'::uuid"
    fi
    state=$(scalar "
      SELECT count(*)::text || E'\\t' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
        WHERE $predicate
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

authenticated_health() {
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  [[ "$(systemctl is-active brains.service)" == active ]]
  curl --fail --silent --max-time 5 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --max-time 5 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
}

for artifact in "${!expected_sha256[@]}"; do
  [[ -f "$artifact" ]]
  [[ "$(sha256sum "$artifact" | awk '{print $1}')" == \
    "${expected_sha256[$artifact]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
unexpected_failed=$(
  systemctl --failed --no-legend --no-pager \
    | awk '{print $2}' \
    | grep -Ev \
      '^(memory-v1-evidence-intake-dispatcher|voice-synthetic-canary)\.service$' \
    || true
)
[[ -z "$unexpected_failed" ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
authenticated_health

phase=clone_verification
PYTHONPATH=. /opt/chat-memory/venv/bin/python -m unittest \
  tests.test_memory_v1_evidence_intake_dispatcher >/dev/null
bash -n "$dispatcher_clone"
bash -n "$clone_test"
bash "$dispatcher_clone" >"$work/dispatcher-clone.out"
grep -qx 'EVIDENCE_INTAKE_DISPATCHER_CLONE=PASS' "$work/dispatcher-clone.out"
RUN_EXACT_PRODUCTION_APPLY=1 RUN_PRIVATE_ENTAILMENT=0 \
  bash "$clone_test" >"$clone_output"
grep -qx 'REVIEWED_OBSERVATION_STAGE_CLONE=PASS' "$clone_output"

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_root/memory_v1_v5_2_reviewed_observation_${run_tag}.status"
report_file="$snapshot_root/memory_v1_v5_2_reviewed_observation_${run_tag}.json"

phase=inventory_timers
while IFS= read -r unit; do
  enabled=$(systemctl is-enabled "$unit" || true)
  active=$(systemctl is-active "$unit" || true)
  [[ "$enabled" == enabled || "$enabled" == disabled ]]
  [[ "$active" == active || "$active" == inactive ]]
  printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" >>"$timer_state"
done < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ -s "$timer_state" ]]

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
partial="$snapshot_root/.memory_pre_v5_2_reviewed_observation_${run_tag}.dump.partial"
backup="$snapshot_root/memory_pre_v5_2_reviewed_observation_${run_tag}.dump"
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
docker exec "$container" psql -X -A -t -U sage -d "$database" -c "
  SELECT table_name
  FROM information_schema.columns
  WHERE table_schema='memory' AND column_name='owner_user_id'
    AND table_name NOT IN (
      'v5_local_packet_stage_admission',
      'v5_local_entailment_assessment',
      'observation_entailment_v5',
      'relational_operation_request',
      'v5_local_inference_event',
      'v5_local_inference_outcome_event'
    )
  ORDER BY table_name
" >"$table_list"
capture_partition target "$target_before"
capture_partition other "$other_before"
qdrant_before=$(qdrant_signature)
other_intended_before=$(other_intended_signature)
other_stage_before=$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_stage_admission
  WHERE owner_user_id<>'$owner'::uuid
")
stage_before=$(scalar 'SELECT count(*) FROM memory.v5_local_packet_stage_admission')
assessment_before=$(scalar 'SELECT count(*) FROM memory.v5_local_entailment_assessment')
entailment_before=$(scalar 'SELECT count(*) FROM memory.observation_entailment_v5')
request_before=$(scalar 'SELECT count(*) FROM memory.relational_operation_request')
claim_before=$(scalar 'SELECT count(*) FROM memory.claim')
projection_before=$(scalar 'SELECT count(*) FROM memory.projection_apply_event')
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_entailment_assessment
  WHERE observation_id=ANY(ARRAY[
    '917ab793-6f03-4af4-847b-c87f5632fa91'::uuid,
    'c0194481-bed5-438f-9407-07e398f14e50'::uuid,
    '14e21c6b-1728-439b-9613-7d9b933d33b8'::uuid
  ])
")" == 0 ]]

phase=install_schema
run_sql <"$migration" >/dev/null
migration_installed=1
[[ "$(scalar "
  SELECT relrowsecurity::text||':'||relforcerowsecurity::text
  FROM pg_class
  WHERE oid='memory.v5_2_reviewed_observation_stage_admission'::regclass
")" == true:true ]]
[[ "$(scalar "
  SELECT rolsuper::text||':'||rolcanlogin::text||':'||
    rolbypassrls::text||':'||rolinherit::text
  FROM pg_roles
  WHERE rolname='memory_v5_2_reviewed_observation_stage_maintainer'
")" == false:false:false:false ]]

phase=owner_isolation_preflight
other_visible=$(
  psql "$POSTGRES_DSN" -X -A -t -q -v ON_ERROR_STOP=1 <<SQL | tail -n 1
BEGIN;
SELECT set_config('app.user_id','$other',true);
SELECT count(*)
FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20)
WHERE route_event_id=ANY(ARRAY[
  '0ee7e138-6fd8-5b4d-80bb-3a627c81301a'::uuid,
  'a8cdb502-f3e9-55dd-a804-a8d3f7096005'::uuid,
  '55b2ab8f-7b3c-5b38-af3c-98a237472082'::uuid
]);
ROLLBACK;
SQL
)
[[ "$other_visible" == 0 ]]

phase=transactional_admission
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 -f "$apply_sql" \
  >"$work/admission.out"
writes_committed=1
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_2_reviewed_observation_stage_admission
  WHERE admission_id=ANY(ARRAY[
    '35febb7e-0993-5ddf-b9b0-71a22d1b8501'::uuid,
    '28f7e091-252e-5357-b64f-144f6445e2e8'::uuid,
    'daa5a659-ba7c-5841-adb2-6aaa7c0481ae'::uuid
  ])
")" == 3 ]]
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_stage_admission')" \
  -eq $((stage_before + 3)) ]]

phase=private_entailment
for sequence in 1 2 3; do
  unit="memory-v1-v5-2-reviewed-observation-production-${sequence}-${run_tag}"
  systemd-run --quiet --wait --pipe --collect --unit="$unit" \
    -p User=ubuntu -p Group=ubuntu \
    -p WorkingDirectory="$repo_root" \
    -p EnvironmentFile="$repo_root/.env" \
    -p LoadCredential=local_api_key:/etc/memory-v1-local-inference/api-key \
    -E PYTHONPATH="$repo_root" \
    -E MEMORY_V1_V5_LOCAL_ENTAILMENT_APPLY=memory_v1_v5_local_entailment_apply_v1 \
    /opt/chat-memory/venv/bin/python \
    scripts/memory_v1_v5_local_entailment_scheduler.py \
    --owner-user-id "$owner" \
    --max-output-tokens 128 --timeout-seconds 300 --apply \
    >"$work/entailment-${sequence}.json"
  jq -e '
    .apply==true
    and .outcome=="observation_entailment_recorded"
    and .governed_decision=="accepted"
    and .database_rows_created==3
    and .local_model_calls==1
    and .external_model_calls==0
    and .zero_write_replay_proved==true
    and .write_counts.claims==0
    and .write_counts.projections==0
    and .write_counts.qdrant==0
    and .write_counts.prompt_influence==0
  ' "$work/entailment-${sequence}.json" >/dev/null
done

phase=verification
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_entailment_assessment')" \
  -eq $((assessment_before + 3)) ]]
[[ "$(scalar 'SELECT count(*) FROM memory.observation_entailment_v5')" \
  -eq $((entailment_before + 3)) ]]
[[ "$(scalar 'SELECT count(*) FROM memory.relational_operation_request')" \
  -eq $((request_before + 3)) ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_local_entailment_assessment
  WHERE governed_decision='accepted'
    AND observation_id=ANY(ARRAY[
      '917ab793-6f03-4af4-847b-c87f5632fa91'::uuid,
      'c0194481-bed5-438f-9407-07e398f14e50'::uuid,
      '14e21c6b-1728-439b-9613-7d9b933d33b8'::uuid
    ])
")" == 3 ]]
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claim_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.projection_apply_event')" \
  == "$projection_before" ]]
capture_partition target "$target_after"
capture_partition other "$other_after"
cmp -s "$target_before" "$target_after"
cmp -s "$other_before" "$other_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]
[[ "$(other_intended_signature)" == "$other_intended_before" ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_stage_admission
  WHERE owner_user_id<>'$owner'::uuid
")" == "$other_stage_before" ]]
authenticated_health

phase=report
jq -n \
  --arg contract_version memory_v1_v5_2_reviewed_observation_production_report_v1 \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg migration_sha256 "${expected_sha256[$migration]}" \
  --arg rollback_sha256 "${expected_sha256[$rollback]}" \
  --arg apply_sha256 "${expected_sha256[$apply_sql]}" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:$contract_version,
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    artifacts:{
      migration_sha256:$migration_sha256,
      rollback_sha256:$rollback_sha256,
      apply_sha256:$apply_sha256
    },
    results:{
      admissions:3,
      entailment_assessments:3,
      accepted:3,
      local_model_calls:3,
      external_model_calls:0,
      claims:0,
      qdrant_writes:0,
      retrieval_changes:0,
      prompt_influence:0,
      replay_zero_write:true,
      cross_owner_isolation:true
    },
    qdrant_sha256:$qdrant_sha256
  }' >"$report_file"
chmod 0600 "$report_file"

phase=restore_timers
restore_timers
phase=complete

printf '%s\n' \
  'REVIEWED_OBSERVATION_PRODUCTION=PASS' \
  "HEAD=$(git rev-parse HEAD)" \
  "BACKUP=$backup" \
  "BACKUP_SHA256=$backup_sha" \
  "REPORT=$report_file" \
  'STAGE_ADMISSIONS=3' \
  'ENTAILMENT_ASSESSMENTS=3' \
  'ENTAILMENT_ACCEPTED=3' \
  'LOCAL_MODEL_CALLS=3' \
  'EXTERNAL_MODEL_CALLS=0' \
  'CLAIMS=0' \
  'QDRANT_WRITES=0' \
  'RETRIEVAL_CHANGES=0' \
  'PROMPT_INFLUENCE=0' \
  'CROSS_OWNER_ISOLATION=PASS' \
  'TIMERS_RESTORED=PASS'
