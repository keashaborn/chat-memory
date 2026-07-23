#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Runs exactly one private V5.2 employment extraction
# against a disposable production clone and writes only a sanitized audit.

if [[ "${MEMORY_V1_V5_2_EMPLOYMENT_CANARY:-}" != authorized ]] && \
   [[ "${MEMORY_V1_V5_2_EMPLOYMENT_TARGET_VERIFY_ONLY:-}" != authorized ]]; then
  echo 'an authorized employment canary or target verification is required' >&2
  exit 1
fi

repo=/opt/chat-memory
runtime_repo=${MEMORY_V1_V5_2_EMPLOYMENT_RUNTIME_REPO:-$repo}
case "$runtime_repo" in
  /opt/chat-memory|/home/ubuntu/chat-memory-v5-2-scheduler) ;;
  *)
    echo 'employment canary runtime repo is not allowlisted' >&2
    exit 1
    ;;
esac
container=brains-postgres-1
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
evidence=3163b69a-f8a8-5263-93d7-300915c2342c
job=c48c5d31-49e0-439d-a031-465ede14d402
content_sha=48c3ea92b1c624634f20a54eb462c6953a11d700eeaa1a2b2cffa3c31f67929d
selector=20260717_v2
owner_sha=9f5d6523a63c8ff7391ecf514fab572af530874832cddc9f56eb3db93cf45b15
evidence_sha=ef829ea1e297118abdae2e3474a6c7db9276b06070a7afcb35ce09b73fcf1c41
port=${MEMORY_V1_V5_2_EMPLOYMENT_CLONE_PORT:-55466}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(docker compose -p memoryv1v52employmentclone \
  -f "$repo/docker-compose.ci.yml" \
  -f "$repo/docker-compose.stage-batch-clone.yml")
clone_dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_employment_canary.lock
timer_state=$(mktemp /tmp/memory-v1-v5-2-employment-timers.XXXXXX)
production_before=$(mktemp /tmp/memory-v1-v5-2-employment-production-before.XXXXXX)
production_after=$(mktemp /tmp/memory-v1-v5-2-employment-production-after.XXXXXX)
other_before=$(mktemp /tmp/memory-v1-v5-2-employment-other-before.XXXXXX)
other_after=$(mktemp /tmp/memory-v1-v5-2-employment-other-after.XXXXXX)
canary_output=$(mktemp /tmp/memory-v1-v5-2-employment-canary.XXXXXX.json)
packet_checks=$(mktemp /tmp/memory-v1-v5-2-employment-packet.XXXXXX.json)
backup=$(mktemp /tmp/memory-v1-v5-2-employment.XXXXXX.dump)
credential=$(mktemp /tmp/memory-v1-v5-2-employment-key.XXXXXX)
chmod 0600 "$timer_state" "$production_before" \
  "$production_after" "$other_before" "$other_after" "$canary_output" \
  "$packet_checks" "$backup" "$credential"

phase=initialization
run_id=
run_tag=
report=
timers_quiesced=0
clone_started=0
semantic_pass=0

production_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

clone_scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

clone_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

capture_production_target() {
  local output=$1
  production_scalar "
    WITH target_rows AS (
      SELECT 'evidence_extraction_job' AS source,
        to_jsonb(value)::text AS row_json
      FROM memory.evidence_extraction_job AS value
      WHERE job_id='$job'::uuid
      UNION ALL
      SELECT 'evidence_extraction_event',to_jsonb(value)::text
      FROM memory.evidence_extraction_event AS value
      WHERE job_id='$job'::uuid
      UNION ALL
      SELECT 'evidence_extraction_packet_v5_local',to_jsonb(value)::text
      FROM memory.evidence_extraction_packet_v5_local AS value
      WHERE job_id='$job'::uuid
      UNION ALL
      SELECT 'evidence_extraction_packet_v5',to_jsonb(value)::text
      FROM memory.evidence_extraction_packet_v5 AS value
      WHERE job_id='$job'::uuid
      UNION ALL
      SELECT 'v5_extraction_call_event',to_jsonb(value)::text
      FROM memory.v5_extraction_call_event AS value
      WHERE job_id='$job'::uuid
      UNION ALL
      SELECT 'v5_local_inference_event',to_jsonb(value)::text
      FROM memory.v5_local_inference_event AS value
      WHERE job_id='$job'::uuid
      UNION ALL
      SELECT 'v5_local_packet_disposition',to_jsonb(value)::text
      FROM memory.v5_local_packet_disposition AS value
      WHERE job_id='$job'::uuid
      UNION ALL
      SELECT 'v5_local_packet_review_artifact',to_jsonb(value)::text
      FROM memory.v5_local_packet_review_artifact AS value
      WHERE job_id='$job'::uuid
      UNION ALL
      SELECT 'v5_local_packet_stage_admission',to_jsonb(value)::text
      FROM memory.v5_local_packet_stage_admission AS value
      WHERE job_id='$job'::uuid
      UNION ALL
      SELECT 'v5_local_entity_validation_assessment',to_jsonb(value)::text
      FROM memory.v5_local_entity_validation_assessment AS value
      WHERE packet_id IN (
        SELECT packet_id
        FROM memory.evidence_extraction_packet_v5_local
        WHERE job_id='$job'::uuid
      )
      UNION ALL
      SELECT 'v5_local_packet_supersession',to_jsonb(value)::text
      FROM memory.v5_local_packet_supersession AS value
      WHERE prior_packet_id IN (
        SELECT packet_id
        FROM memory.evidence_extraction_packet_v5_local
        WHERE job_id='$job'::uuid
      ) OR replacement_packet_id IN (
        SELECT packet_id
        FROM memory.evidence_extraction_packet_v5_local
        WHERE job_id='$job'::uuid
      )
    )
    SELECT source || E'\\t' || row_json
    FROM target_rows
    ORDER BY source,row_json" >"$output"
  chmod 0600 "$output"
}

capture_clone_other_owners() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(clone_scalar "
      SELECT count(*)::text || E'\\t' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
        WHERE owner_user_id<>'$owner'::uuid
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(clone_scalar "
    SELECT table_name
    FROM information_schema.columns
    WHERE table_schema='memory' AND column_name='owner_user_id'
    ORDER BY table_name")
  chmod 0600 "$output"
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
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    if [[ "$enabled" == enabled ]]; then
      sudo -n systemctl enable "$unit" >/dev/null
    else
      [[ "$enabled" == disabled ]]
      sudo -n systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      [[ "$active" == inactive ]]
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

cleanup() {
  exit_code=$?
  restore_timers || exit_code=1
  if [[ "$clone_started" -eq 1 ]]; then
    "${compose[@]}" down -v >/dev/null 2>&1 || exit_code=1
  fi
  if [[ "$exit_code" -ne 0 && -n "$run_tag" && ! -e "$report" ]]; then
    failure_report="$snapshot_dir/memory_v1_v5_2_employment_canary_harness_failure_${run_tag}.json"
    jq -n --arg phase "$phase" --arg completed_at \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      '{contract_version:
          "memory_v1_v5_2_employment_canary_harness_failure_v1",
        completed_at:$completed_at,outcome:"harness_error",
        failure_phase:$phase,external_model_calls:0,
        hard_stop:"no automatic retry"}' >"$failure_report" || true
    chmod 0600 "$failure_report" 2>/dev/null || true
    sha256sum "$failure_report" >"$failure_report.sha256" 2>/dev/null || true
    chmod 0600 "$failure_report.sha256" 2>/dev/null || true
  fi
  rm -f "$timer_state" "$production_before" \
    "$production_after" "$other_before" "$other_after" "$canary_output" \
    "$packet_checks" "$backup" "$credential"
  exit "$exit_code"
}
trap cleanup EXIT

if [[ "${MEMORY_V1_V5_2_EMPLOYMENT_TARGET_VERIFY_ONLY:-}" == authorized ]]; then
  phase=target_isolation_zero_call_test
  capture_production_target "$production_before"
  sleep 2
  capture_production_target "$production_after"
  cmp -s "$production_before" "$production_after"
  printf '%s\n' \
    'memory_v1_v5_2_employment_target_isolation: PASS' \
    'local_model_calls=0' \
    'external_model_calls=0'
  exit 0
fi

phase=preflight
cd "$repo"
git merge-base --is-ancestor e05d3d8 HEAD
git -C "$runtime_repo" merge-base --is-ancestor e05d3d8 HEAD
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-scheduler.timer)" == active ]]
grep -q -- '--contract-profile v5_1' \
  /etc/systemd/system/memory-v1-v5-local-inference-scheduler.service
[[ "$(production_scalar "
  SELECT count(*)
  FROM memory.v5_local_inference_event
  WHERE owner_user_id='$owner'::uuid
    AND action='reserved'
    AND created_at>=clock_timestamp()-interval '24 hours'")" -lt 12 ]]
[[ "$(production_scalar "
  SELECT count(*)
  FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id='$evidence'::uuid
    AND job_id='$job'::uuid
    AND evidence_content_sha256='$content_sha'
    AND selector_version='$selector'
    AND status='review_required'
    AND attempts=1")" == 1 ]]
[[ "$(production_scalar "
  SELECT count(*)
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND job_id='$job'::uuid")" == 1 ]]
sudo -n test -r /etc/memory-v1-local-inference/api-key
sudo -n cat /etc/memory-v1-local-inference/api-key >"$credential"
[[ "$(wc -c <"$credential")" -ge 32 ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$runtime_repo" rev-parse --short=12 HEAD)"
report="$snapshot_dir/memory_v1_v5_2_employment_canary_${run_tag}.json"

phase=quiesce_production_timers
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$timer_state" ]]
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  related_service=${unit%.timer}.service
  for _attempt in $(seq 1 60); do
    systemctl is-active --quiet "$related_service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$related_service"
done <"$timer_state"

phase=production_baseline
capture_production_target "$production_before"
[[ -s "$production_before" ]]
qdrant_before=$(qdrant_signature)

phase=create_disposable_clone
docker exec "$container" pg_dump -U sage -d memory \
  -Fc >"$backup"
[[ -s "$backup" ]]
"${compose[@]}" up -d --wait postgres
clone_started=1
memory_role_sql=$(production_scalar "
  SELECT format(
    'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;',
    rolname)
  FROM pg_roles
  WHERE (rolname LIKE 'memory\\_%' ESCAPE '\\'
         OR rolname='lifeswitch_training_observation_owner')
  ORDER BY rolname")
[[ -n "$memory_role_sql" ]]
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  "$memory_role_sql" | clone_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"
[[ "$(clone_scalar "SELECT count(*) FROM memory.evidence_extraction_job")" \
  == "$(production_scalar "SELECT count(*) FROM memory.evidence_extraction_job")" ]]

phase=prepare_exact_clone_target
clone_sql >/dev/null <<SQL
BEGIN;
SET LOCAL session_replication_role=replica;
DELETE FROM memory.v5_local_entity_validation_assessment
WHERE owner_user_id='$owner'::uuid AND packet_id IN (
  SELECT packet_id FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid);
DELETE FROM memory.v5_local_packet_disposition
WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid;
DELETE FROM memory.v5_local_packet_review_artifact
WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid;
DELETE FROM memory.v5_local_packet_stage_admission
WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid;
DELETE FROM memory.v5_local_packet_supersession
WHERE owner_user_id='$owner'::uuid AND (
  prior_packet_id IN (
    SELECT packet_id FROM memory.evidence_extraction_packet_v5_local
    WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid)
  OR replacement_packet_id IN (
    SELECT packet_id FROM memory.evidence_extraction_packet_v5_local
    WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid));
DELETE FROM memory.evidence_extraction_packet_v5_local
WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid;
DELETE FROM memory.evidence_extraction_packet_v5
WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid;
DELETE FROM memory.v5_extraction_call_event
WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid;
DELETE FROM memory.v5_local_inference_event
WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid;
DELETE FROM memory.evidence_extraction_event
WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid;
UPDATE memory.evidence_extraction_job
SET status='pending',attempts=0,available_at=clock_timestamp(),
    lease_token=NULL,lease_expires_at=NULL,worker_id=NULL,last_error=NULL,
    result='{}'::jsonb,updated_at=clock_timestamp(),
    checkpoint_sequence=0,checkpoint_sha256=NULL
WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid;
SET LOCAL session_replication_role=origin;
COMMIT;
SQL
[[ "$(clone_scalar "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    AND status='pending' AND attempts=0
    AND evidence_content_sha256='$content_sha'")" == 1 ]]
[[ "$(clone_scalar "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid")" == 0 ]]
capture_clone_other_owners "$other_before"

phase=one_private_local_call
set +e
POSTGRES_DSN="$clone_dsn" \
PYTHONPATH="$runtime_repo" \
MEMORY_V1_V5_LOCAL_INFERENCE_APPLY=memory_v1_v5_local_inference_canary_apply_v1 \
MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(<"$credential")" \
  "$repo/venv/bin/python" \
  "$runtime_repo/scripts/memory_v1_v5_local_inference_canary.py" \
  --owner-user-id "$owner" \
  --evidence-id "$evidence" \
  --expected-job-id "$job" \
  --expected-content-sha256 "$content_sha" \
  --selector-version "$selector" \
  --contract-profile v5_2 \
  --run-id "$run_id" \
  --endpoint http://127.0.0.1:18080/v1/chat/completions \
  --worker-id memory_v1_v5_2_employment_clone_canary \
  --lease-seconds 900 \
  --max-attempts 1 \
  --timeout-seconds 600 \
  --max-output-tokens 4096 \
  --rolling-window-seconds 86400 \
  --max-reserved-jobs 12 \
  --failure-threshold 3 \
  --apply >"$canary_output" 2>&1
canary_status=$?
set -e
[[ "$canary_status" -eq 0 || "$canary_status" -eq 1 ]]
jq -e '
  .contract_version=="memory_v1_v5_local_inference_canary_v1" and
  .predicate_contract_profile=="v5_2" and
  .extraction_contract_version=="memory_v1_relational_extraction_v5_2" and
  .predicate_registry_version=="memory_predicate_registry_v5_2" and
  (.outcome=="rejected" or
    .audit.policy_compiler_version==
      "memory_v1_semantic_policy_compiler_v6") and
  .external_model_calls==0 and .local_model_calls<=1 and
  .write_counts.claims==0 and .write_counts.qdrant==0 and
  .write_counts.prompt_influence==0
' "$canary_output" >/dev/null

phase=verify_clone_result
if [[ "$canary_status" -eq 0 ]] && \
   [[ "$(jq -r '.outcome' "$canary_output")" == accepted ]]; then
  clone_scalar "
    WITH packet AS (
      SELECT normalized_packet
      FROM memory.evidence_extraction_packet_v5_local
      WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    ),
    self_entity AS (
      SELECT item->>'entity_ref' AS entity_ref
      FROM packet,jsonb_array_elements(
        normalized_packet->'entity_mentions') AS item
      WHERE item->>'entity_type'='self'
        AND item->>'mention_kind'='self_reference'
    ),
    employer AS (
      SELECT item->>'entity_ref' AS entity_ref
      FROM packet,jsonb_array_elements(
        normalized_packet->'entity_mentions') AS item
      WHERE item->>'entity_type'='organization'
        AND item->>'name_text'='Wisconsin Early Autism Project'
    ),
    observation AS (
      SELECT item
      FROM packet,jsonb_array_elements(
        normalized_packet->'observations') AS item
    )
    SELECT jsonb_build_object(
      'entity_mentions',jsonb_array_length(
        normalized_packet->'entity_mentions'),
      'observations',jsonb_array_length(
        normalized_packet->'observations'),
      'comparison_hints',jsonb_array_length(
        normalized_packet->'comparison_hints'),
      'deferrals',jsonb_array_length(normalized_packet->'deferrals'),
      'packet_findings',jsonb_array_length(
        normalized_packet->'packet_findings'),
      'self_entity',(SELECT count(*)=1 FROM self_entity),
      'organization_entity',(SELECT count(*)=1 FROM employer),
      'organization_name_exact',(SELECT count(*)=1 FROM employer),
      'employment_link',(
        SELECT count(*)=1
        FROM observation o
        WHERE o.item->>'predicate'='employment.worked_for'
          AND o.item->>'subject_entity_ref'=(
            SELECT entity_ref FROM self_entity)
          AND o.item->'object'->>'kind'='entity'
          AND o.item->'object'->>'entity_ref'=(
            SELECT entity_ref FROM employer)),
      'observation_predicates',(
        SELECT jsonb_agg(item->>'predicate' ORDER BY item->>'predicate')
        FROM observation),
      'no_current_employment_invention',(
        SELECT count(*)=1
        FROM observation o
        WHERE o.item->>'predicate'='employment.worked_for'
          AND o.item->'temporal'->>'semantic'='state_validity'
          AND o.item->'temporal'->>'shape'='none'
          AND o.item->'temporal'->>'certainty'='unknown'),
      'no_date_invention',(
        SELECT count(*)=1
        FROM observation o
        WHERE o.item->>'predicate'='employment.worked_for'
          AND o.item->'temporal'->'instant'='null'::jsonb
          AND o.item->'temporal'->'calendar_range'='null'::jsonb
          AND o.item->'temporal'->'instant_range'='null'::jsonb
          AND o.item->'temporal'->'relative_offset'='null'::jsonb
          AND o.item->'temporal'->'recurrence'='null'::jsonb
          AND o.item->'temporal'->>'anchored_to_source_time'='false'))
    FROM packet" >"$packet_checks"
  if jq -e '
    .entity_mentions==2 and .observations==1 and
    .comparison_hints==0 and .deferrals==0 and .packet_findings==0 and
    .self_entity==true and .organization_entity==true and
    .organization_name_exact==true and .employment_link==true and
    .observation_predicates==["employment.worked_for"] and
    .no_current_employment_invention==true and
    .no_date_invention==true
  ' "$packet_checks" >/dev/null; then
    semantic_pass=1
  fi
fi

capture_clone_other_owners "$other_after"
cmp -s "$other_before" "$other_after"

phase=verify_production_unchanged
capture_production_target "$production_after"
cmp -s "$production_before" "$production_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]
restore_timers
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-scheduler.timer)" == active ]]
grep -q -- '--contract-profile v5_1' \
  /etc/systemd/system/memory-v1-v5-local-inference-scheduler.service

phase=write_sanitized_audit
if [[ "$canary_status" -eq 0 ]] && \
   [[ "$(jq -r '.outcome' "$canary_output")" == accepted ]] && \
   [[ "$semantic_pass" -eq 1 ]]; then
  jq -n \
    --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg owner_sha "$owner_sha" --arg evidence_sha "$evidence_sha" \
    --arg content_sha "$content_sha" --arg qdrant_sha "$qdrant_after" \
    --slurpfile result "$canary_output" --slurpfile checks "$packet_checks" \
    '{contract_version:"memory_v1_v5_2_employment_canary_report_v1",
      completed_at:$completed_at,outcome:"pass",
      owner_user_id_sha256:$owner_sha,evidence_id_sha256:$evidence_sha,
      evidence_content_sha256:$content_sha,
      predicate_contract_profile:"v5_2",
      extraction_contract_version:"memory_v1_relational_extraction_v5_2",
      predicate_registry_version:"memory_predicate_registry_v5_2",
      policy_compiler_version:$result[0].audit.policy_compiler_version,
      local_model_calls:$result[0].local_model_calls,
      external_model_calls:$result[0].external_model_calls,
      counts:{entity_mentions:$checks[0].entity_mentions,
        observations:$checks[0].observations,
        comparison_hints:$checks[0].comparison_hints,
        deferrals:$checks[0].deferrals,
        packet_findings:$checks[0].packet_findings},
      observation_predicates:$checks[0].observation_predicates,
      checks:{self_entity:$checks[0].self_entity,
        organization_entity:$checks[0].organization_entity,
        organization_name_exact:$checks[0].organization_name_exact,
        employment_link:$checks[0].employment_link,
        no_current_employment_invention:
          $checks[0].no_current_employment_invention,
        no_date_invention:$checks[0].no_date_invention,
        zero_write_replay:$result[0].zero_write_replay_proved,
        account_isolation:true,qdrant_unchanged:true,
        production_memory_unchanged:true,timers_restored:true},
      production_write_counts:{queue:0,ledger:0,packets:0,claims:0,
        qdrant:0,retrieval:0,prompt_influence:0},
      qdrant_sha256:$qdrant_sha,
      hard_stop:"before recurring V5.2 scheduler activation"}' >"$report"
else
  jq -n \
    --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg owner_sha "$owner_sha" --arg evidence_sha "$evidence_sha" \
    --arg content_sha "$content_sha" --arg qdrant_sha "$qdrant_after" \
    --slurpfile result "$canary_output" --slurpfile checks "$packet_checks" \
    '{contract_version:"memory_v1_v5_2_employment_canary_report_v1",
      completed_at:$completed_at,outcome:"rejected",
      provider_outcome:($result[0].outcome // "unknown"),
      owner_user_id_sha256:$owner_sha,evidence_id_sha256:$evidence_sha,
      evidence_content_sha256:$content_sha,
      predicate_contract_profile:"v5_2",
      extraction_contract_version:"memory_v1_relational_extraction_v5_2",
      predicate_registry_version:"memory_predicate_registry_v5_2",
      local_model_calls:($result[0].local_model_calls // 0),
      external_model_calls:($result[0].external_model_calls // 0),
      rejection_code:(
        if $result[0].outcome=="accepted"
        then "employment_expectation_mismatch"
        else ($result[0].rejection_code // "unknown_rejection") end),
      observed_counts:(
        if ($checks|length)==1 then {
          entity_mentions:$checks[0].entity_mentions,
          observations:$checks[0].observations,
          comparison_hints:$checks[0].comparison_hints,
          deferrals:$checks[0].deferrals,
          packet_findings:$checks[0].packet_findings
        } else null end),
      observation_predicates:(
        if ($checks|length)==1
        then $checks[0].observation_predicates else null end),
      expectation_checks:(
        if ($checks|length)==1 then {
          self_entity:$checks[0].self_entity,
          organization_entity:$checks[0].organization_entity,
          organization_name_exact:$checks[0].organization_name_exact,
          employment_link:$checks[0].employment_link,
          no_current_employment_invention:
            $checks[0].no_current_employment_invention,
          no_date_invention:$checks[0].no_date_invention
        } else null end),
      validation_error_types:($result[0].audit.validation_error_types // []),
      validation_error_locations:
        ($result[0].audit.validation_error_locations // []),
      checks:{account_isolation:true,qdrant_unchanged:true,
        production_memory_unchanged:true,timers_restored:true},
      production_write_counts:{queue:0,ledger:0,packets:0,claims:0,
        qdrant:0,retrieval:0,prompt_influence:0},
      qdrant_sha256:$qdrant_sha,
      hard_stop:"rejected before recurring V5.2 scheduler activation"}' \
    >"$report"
fi
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=delete_disposable_clone
"${compose[@]}" down -v >/dev/null
clone_started=0
if [[ "$(jq -r '.outcome' "$report")" != pass ]]; then
  printf 'memory_v1_v5_2_employment_canary: REJECTED\n'
  printf 'report=%s\n' "$report"
  exit 1
fi
printf 'memory_v1_v5_2_employment_canary: PASS\n'
printf 'report=%s\n' "$report"
