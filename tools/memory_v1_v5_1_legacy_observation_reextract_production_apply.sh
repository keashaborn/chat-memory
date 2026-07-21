#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the additive owner-scoped V5-to-V5.1
# re-extraction lane and queues exactly three hash-reviewed evidence records.

if [[ "${MEMORY_V1_V5_1_LEGACY_OBSERVATION_REEXTRACT_PRODUCTION_APPLY:-}" != authorized ]]; then
  echo 'production apply authorization token is required' >&2
  exit 1
fi

repo=/opt/chat-memory
python=/opt/chat-memory/venv/bin/python
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
selector=20260721_v5_1_legacy_observation_reextract_v1
evidence_a=04e5a4c2-8742-5f1f-ace2-d4de9f5e94da
evidence_b=7877ebf3-1c5a-4bd2-8b43-9514a88a0e12
evidence_c=aaaa0d55-0563-4197-8c8e-a657240e6cc1
migration=ops/sql/20260721_memory_v1_v5_1_legacy_observation_reextract.sql
test_sql=tests/memory_v1_v5_1_legacy_observation_reextract.sql
worker=scripts/memory_v1_v5_1_legacy_observation_reextract.py
worker_test=scripts/memory_v1_v5_1_legacy_observation_reextract_test.py
snapshot_dir=/home/ubuntu/brains/snapshots
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo" rev-parse --short=12 HEAD)"
backup_partial="$snapshot_dir/.memory_pre_v5_1_observation_reextract_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_1_observation_reextract_${run_id}.dump"
dry="$snapshot_dir/memory_v1_v5_1_observation_reextract_dry_${run_id}.json"
applied="$snapshot_dir/memory_v1_v5_1_observation_reextract_applied_${run_id}.json"
report="$snapshot_dir/memory_v1_v5_1_observation_reextract_production_${run_id}.json"
timer_state=$(mktemp /tmp/memory-v1-v5-1-observation-reextract-timers.XXXXXX)
phase=initialize
timers_quiesced=0

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    if [[ "$enabled" == enabled ]]; then
      sudo -n systemctl enable "$unit" >/dev/null
    else
      sudo -n systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
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
  rm -f "$timer_state" "$backup_partial" "$dry.stdout" "$applied.stdout"
  if (( rc != 0 )); then
    printf 'memory_v1_v5_1_legacy_observation_reextract_production_apply: FAIL phase=%s rc=%s\n' "$phase" "$rc" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT

scalar() {
  docker exec "$container" psql -X -A -t -U sage -d "$database" -c "$1"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

authenticated_health() {
  set -a
  source "$repo/.env"
  set +a
  [[ "$(systemctl is-active brains.service)" == active ]]
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz | jq -e '.ok==true and .postgres==true' >/dev/null
}

cd "$repo"
[[ -z "$(git status --porcelain)" ]]
phase=source_preflight
bash -n tools/memory_v1_v5_1_legacy_observation_reextract_production_apply.sh
PYTHONPATH="$repo" "$python" "$worker_test" >/dev/null
"$python" -m py_compile "$worker"
sha256sum -c <<'HASHES'
55f06c78b4cbe32ccdc23e0643cd73b17e124e07d079115cd8ef1fa26c6d5185  ops/sql/20260721_memory_v1_v5_1_legacy_observation_reextract.sql
4b048c622f0937db48b3677a4202140df0e9ba872a9b53886351c8f26ba34d39  tests/memory_v1_v5_1_legacy_observation_reextract.sql
7c932de9cf3e6c63e5953ee31745e3955dab156cd0f8967a5214b72304645d8d  scripts/memory_v1_v5_1_legacy_observation_reextract.py
0bbf48838b16feef8d192ff5eda80d576b3a5728a484aa210d3d8fc77ae62b86  scripts/memory_v1_v5_1_legacy_observation_reextract_test.py
HASHES
authenticated_health

phase=quiesce_timers
while read -r unit; do
  enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)
  active=$(systemctl is-active "$unit" 2>/dev/null || true)
  [[ "$enabled" == enabled || "$enabled" == disabled ]]
  [[ "$active" == active || "$active" == inactive ]]
  printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" >>"$timer_state"
  sudo -n systemctl stop "$unit"
done < <(systemctl list-unit-files --no-legend 'memory-v1-*.timer' | awk '{print $1}' | sort)
chmod 0600 "$timer_state"
timers_quiesced=1

phase=backup
docker exec "$container" pg_dump -U sage -d "$database" -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
claims_before=$(scalar 'SELECT count(*) FROM memory.claim')
qdrant_before=$(qdrant_signature)
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector'")" == 0 ]]

phase=install_and_rollback_security_test
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" <"$migration" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" <"$migration" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
  -v owner_a="$owner" -v owner_b="$other" -v target_evidence="$evidence_a" \
  <"$test_sql" >/dev/null
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

set -a
source "$repo/.env"
set +a
common=(
  --owner-user-id "$owner"
  --evidence-id "$evidence_a"
  --evidence-id "$evidence_b"
  --evidence-id "$evidence_c"
)
phase=dry_run
PYTHONPATH="$repo" "$python" "$worker" "${common[@]}" \
  --report-path "$dry" >"$dry.stdout"
jq -e '.apply==false and .plan_count==3 and .applied==0 and .replayed==0 and
  .claim_writes==0 and .qdrant_writes==0 and .external_model_calls==0 and
  .local_model_calls==0 and .prompt_influence==0' "$dry.stdout" >/dev/null
plan_sha=$(jq -r '.plan_sha256' "$dry.stdout")

phase=transactional_apply
MEMORY_V1_V5_1_LEGACY_OBSERVATION_REEXTRACT_APPLY=memory_v1_v5_1_legacy_observation_reextract_apply_v1 \
PYTHONPATH="$repo" "$python" "$worker" "${common[@]}" \
  --expected-plan-sha256 "$plan_sha" --apply --report-path "$applied" \
  >"$applied.stdout"
jq -e '.apply==true and .plan_count==3 and .applied==3 and .replayed==3 and
  .claim_writes==0 and .qdrant_writes==0 and .external_model_calls==0 and
  .local_model_calls==0 and .prompt_influence==0' "$applied.stdout" >/dev/null

phase=postflight
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE owner_user_id='$owner'::uuid AND selector_version='$selector' AND status='pending' AND attempts=0")" == 3 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal WHERE owner_user_id='$owner'::uuid AND selector_version='$selector'")" == 3 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event event JOIN memory.evidence_extraction_job job USING(owner_user_id,job_id) WHERE job.owner_user_id='$owner'::uuid AND job.selector_version='$selector' AND event.event_type='queued'")" == 3 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE owner_user_id<>'$owner'::uuid AND selector_version='$selector'")" == 0 ]]
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=restore_timers_and_health
restore_timers
authenticated_health

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg plan_sha256 "$plan_sha" --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_1_legacy_observation_reextract_production_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    scope:{owner_count:1,evidence_count:3,selector_version:"20260721_v5_1_legacy_observation_reextract_v1",plan_sha256:$plan_sha256},
    checks:{hash_locked:true,rollback_security_test:true,transactional_apply:true,
      replay_proved:true,cross_owner_rows:0,claims_written:0,qdrant_writes:0,
      external_model_calls:0,local_model_calls:0,prompt_influence:0,
      timer_states_restored:true,service_healthy:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_local_inference_or_downstream_staging"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_v5_1_legacy_observation_reextract_production_apply: PASS\nreport=%s\nbackup=%s\n' "$report" "$backup"
