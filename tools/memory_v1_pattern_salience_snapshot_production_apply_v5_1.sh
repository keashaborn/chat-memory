#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies exactly one reviewed owner-scoped V5.1 snapshot
# manifest, proves zero-write replay, and stops before pattern/runtime activation.

repo=/opt/chat-memory
container=brains-postgres-1
database=memory
database_role=sage
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
authorized_base=76cc4736dad0bc2d8ebca3534b9d4d8a7cf299a7
apply_base=91d2b94942388659117e85308ecffa74bb8fdd57
snapshot_dir=/home/ubuntu/brains/snapshots
manifest=$snapshot_dir/memory_v1_pattern_salience_snapshot_apply_manifest_20260720_91d2b9494238.json
manifest_file_sha=4525bd67e72c60cab82d79ae67534e0f376a0dd75aab50de3ed1a8d5d5b0ca67
manifest_sha=3fbd91e86eb95af763446af4829a7d7bc3d28baa0e67ad4051b567bd9de9662d
apply_runner=scripts/memory_v1_pattern_salience_snapshot_apply_v5_1.py
lock_file=/home/ubuntu/brains/.memory_v1_pattern_salience_snapshot_apply_v5_1.lock
env_file=/opt/chat-memory/.env
phase=initialization
timers_quiesced=0
timer_state=
status_file=
timers=()

restore_timers() {
  [[ "$timers_quiesced" -eq 1 && -s "$timer_state" ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]] || return 1
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]] || return 1
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      [[ "$active" == inactive ]] || return 1
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  exit_code=$?
  if [[ "$timers_quiesced" -eq 1 ]]; then
    failed_phase=$phase
    phase=restore_timers_after_failure
    restore_timers || exit_code=1
    phase=$failed_phase
  fi
  if [[ -n "$status_file" ]]; then
    {
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U "$database_role" -d "$database" -c "$1"
}

authenticated_health() {
  set -a
  source "$env_file"
  set +a
  test -n "${VS_SERVICE_TOKEN:-}"
  [[ "$(systemctl is-active brains.service)" == active ]]
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_memory_state() {
  local table_list=$1 output=$2 state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || return 1
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

phase=source_preflight
cd "$repo"
git merge-base --is-ancestor "$authorized_base" HEAD
git merge-base --is-ancestor "$apply_base" HEAD
[[ -z "$(git status --porcelain)" ]]
sha256sum -c <<'HASHES'
15e03d01a885cb2e198670978769d3777a931d1c2817658b297a1f869b0b4f63  scripts/memory_v1_pattern_salience_snapshot_apply_v5_1.py
HASHES
[[ "$(sha256sum "$manifest" | awk '{print $1}')" == "$manifest_file_sha" ]]
jq -e --arg sha "$manifest_sha" '
  .contract_version=="memory_v1_pattern_salience_snapshot_apply_manifest_v5_1" and
  .manifest_sha256==$sha and (.items|length)==6 and
  .write_budget.maximum_total_rows==30 and
  .write_budget.other_owners==0 and
  .write_budget.pattern_heads_or_revisions==0 and
  .write_budget.qdrant==0 and .write_budget.prompt_influence==0
' "$manifest" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
execution_head=$(git rev-parse HEAD)
status_file="$snapshot_dir/memory_v1_pattern_salience_snapshot_apply_${run_id}.status"
timer_state="$snapshot_dir/memory_v1_pattern_salience_snapshot_apply_timers_before_${run_id}.tsv"

phase=database_preflight
[[ "$(psql_scalar "SELECT current_setting('server_version_num')::integer/10000")" == 16 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.epistemic_target_binding_v5_1")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.epistemic_assessment_snapshot_v5_1")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.epistemic_assessment_observation_link_v5_1")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.salience_feature_snapshot_v5_1")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.epistemic_operation_request_v5_1")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.retrieval_outcome_signal_v5_1")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.pattern_hypothesis_v5_1")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.pattern_hypothesis_revision_v5_1")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.pattern_review_v5_1")" == 0 ]]
authenticated_health

phase=capture_timer_state
mapfile -t timers < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ ${#timers[@]} -gt 0 ]]
: >"$timer_state"
for unit in "${timers[@]}"; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done
chmod 0600 "$timer_state"

phase=quiesce_memory_v1_timers
for unit in "${timers[@]}"; do sudo -n systemctl stop "$unit"; done
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done <"$timer_state"

phase=capture_baseline
table_list="$snapshot_dir/memory_v1_pattern_salience_snapshot_apply_tables_${run_id}.txt"
before_state="$snapshot_dir/memory_v1_pattern_salience_snapshot_apply_before_${run_id}.tsv"
after_preflight_state="$snapshot_dir/memory_v1_pattern_salience_snapshot_apply_after_preflight_${run_id}.tsv"
after_apply_state="$snapshot_dir/memory_v1_pattern_salience_snapshot_apply_after_${run_id}.tsv"
after_replay_state="$snapshot_dir/memory_v1_pattern_salience_snapshot_apply_after_replay_${run_id}.tsv"
psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" >"$table_list"
chmod 0600 "$table_list"
capture_memory_state "$table_list" "$before_state"
qdrant_before=$(qdrant_signature)

phase=fresh_backup
backup_partial="$snapshot_dir/.memory_pre_pattern_salience_snapshot_apply_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_pattern_salience_snapshot_apply_${run_id}.dump"
catalog="$backup.catalog"
checksum="$backup.sha256"
docker exec "$container" pg_dump -U "$database_role" -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$checksum"
chmod 0600 "$checksum"

phase=revalidate_before_apply
git merge-base --is-ancestor "$apply_base" HEAD
[[ -z "$(git status --porcelain)" ]]
sha256sum -c <<'HASHES'
15e03d01a885cb2e198670978769d3777a931d1c2817658b297a1f869b0b4f63  scripts/memory_v1_pattern_salience_snapshot_apply_v5_1.py
HASHES
[[ "$(sha256sum "$manifest" | awk '{print $1}')" == "$manifest_file_sha" ]]

set -a
source "$env_file"
set +a
preflight_result="$snapshot_dir/memory_v1_pattern_salience_snapshot_preflight_${run_id}.json"
apply_result="$snapshot_dir/memory_v1_pattern_salience_snapshot_apply_result_${run_id}.json"
replay_result="$snapshot_dir/memory_v1_pattern_salience_snapshot_replay_result_${run_id}.json"

phase=rollback_only_preflight
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo" venv/bin/python "$apply_runner" \
  --mode preflight --manifest "$manifest" --output "$preflight_result" >/dev/null
capture_memory_state "$table_list" "$after_preflight_state"
cmp -s "$before_state" "$after_preflight_state"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=durable_bounded_apply
MEMORY_V1_PATTERN_SALIENCE_SNAPSHOT_APPLY=authorized \
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo" \
  venv/bin/python "$apply_runner" --mode apply --manifest "$manifest" \
  --output "$apply_result" >/dev/null
jq -e '
  .mode=="apply" and .persistent_writes==30 and
  (.results|length)==6 and all(.results[];.apply_outcome=="applied")
' "$apply_result" >/dev/null

phase=bounded_apply_verification
counts=$(psql_scalar "SELECT jsonb_build_object(
  'bindings',(SELECT count(*) FROM memory.epistemic_target_binding_v5_1),
  'assessments',(SELECT count(*) FROM memory.epistemic_assessment_snapshot_v5_1),
  'links',(SELECT count(*) FROM memory.epistemic_assessment_observation_link_v5_1),
  'features',(SELECT count(*) FROM memory.salience_feature_snapshot_v5_1),
  'requests',(SELECT count(*) FROM memory.epistemic_operation_request_v5_1),
  'signals',(SELECT count(*) FROM memory.retrieval_outcome_signal_v5_1),
  'pattern_heads',(SELECT count(*) FROM memory.pattern_hypothesis_v5_1),
  'pattern_revisions',(SELECT count(*) FROM memory.pattern_hypothesis_revision_v5_1),
  'pattern_links',(SELECT count(*) FROM memory.pattern_observation_link_v5_1),
  'pattern_reviews',(SELECT count(*) FROM memory.pattern_review_v5_1),
  'pattern_review_links',(SELECT count(*) FROM memory.pattern_review_observation_v5_1),
  'pattern_requests',(SELECT count(*) FROM memory.pattern_operation_request_v5_1),
  'pattern_events',(SELECT count(*) FROM memory.pattern_apply_event_v5_1))")
jq -e '
  .bindings==6 and .assessments==6 and .links==6 and .features==6 and
  .requests==6 and .signals==0 and .pattern_heads==0 and
  .pattern_revisions==0 and .pattern_links==0 and .pattern_reviews==0 and
  .pattern_review_links==0 and .pattern_requests==0 and .pattern_events==0
' <<<"$counts" >/dev/null
[[ "$(psql_scalar "SELECT count(DISTINCT owner_user_id) FROM memory.epistemic_target_binding_v5_1")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.epistemic_target_binding_v5_1 WHERE owner_user_id<>'$owner'::uuid")" == 0 ]]
capture_memory_state "$table_list" "$after_apply_state"
for file in "$before_state" "$after_apply_state"; do
  grep -Ev '^(epistemic_target_binding_v5_1|epistemic_assessment_snapshot_v5_1|epistemic_assessment_observation_link_v5_1|salience_feature_snapshot_v5_1|epistemic_operation_request_v5_1)[[:space:]]' "$file" >"$file.untouched"
done
cmp -s "$before_state.untouched" "$after_apply_state.untouched"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=zero_write_replay
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo" venv/bin/python "$apply_runner" \
  --mode replay --manifest "$manifest" --apply-result "$apply_result" \
  --output "$replay_result" >/dev/null
jq -e '
  .mode=="replay" and .persistent_writes==0 and
  (.results|length)==6 and all(.results[];.apply_outcome=="replayed")
' "$replay_result" >/dev/null
capture_memory_state "$table_list" "$after_replay_state"
cmp -s "$after_apply_state" "$after_replay_state"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=restore_timer_state
restore_timers
timer_after="$snapshot_dir/memory_v1_pattern_salience_snapshot_apply_timers_after_${run_id}.tsv"
: >"$timer_after"
for unit in "${timers[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_after"
done
chmod 0600 "$timer_after"
cmp -s "$timer_state" "$timer_after"

phase=service_health
authenticated_health

phase=report
report="$snapshot_dir/memory_v1_pattern_salience_snapshot_production_apply_${run_id}.json"
HEAD="$execution_head" BACKUP="$backup" CHECKSUM="$checksum" \
MANIFEST="$manifest" APPLY_RESULT="$apply_result" REPLAY_RESULT="$replay_result" \
COUNTS="$counts" python3 - "$report" <<'PY'
import hashlib,json,os,sys
from datetime import datetime,timezone
from pathlib import Path
path=Path(sys.argv[1]); manifest=json.loads(Path(os.environ['MANIFEST']).read_text())
value={
  'contract_version':'memory_v1_pattern_salience_snapshot_production_apply_report_v5_1',
  'completed_at':datetime.now(timezone.utc).isoformat(),
  'source_commit':os.environ['HEAD'],
  'owner_user_id_sha256':manifest['owner_user_id_sha256'],
  'manifest_sha256':manifest['manifest_sha256'],
  'backup':os.environ['BACKUP'],
  'backup_sha256':Path(os.environ['CHECKSUM']).read_text().split()[0],
  'apply_result':os.environ['APPLY_RESULT'],
  'replay_result':os.environ['REPLAY_RESULT'],
  'final_counts':json.loads(os.environ['COUNTS']),
  'checks':{
    'fresh_backup_verified':True,
    'all_discovered_memory_v1_timer_states_restored_exactly':True,
    'rollback_only_preflight_passed':True,
    'exactly_30_owner_scoped_append_only_rows_created':True,
    'zero_write_replay_passed':True,
    'other_owner_rows_written':0,
    'pattern_heads_revisions_reviews_or_events_written':0,
    'retrieval_outcome_rows_written':0,
    'non_target_tables_unchanged':True,
    'qdrant_unchanged':True,
    'brains_health_and_readiness_passed':True,
    'external_or_local_model_calls':0,
    'retrieval_or_prompt_influence':False,
  },
  'hard_stop':'before_controlled_pattern_apply_background_jobs_retrieval_or_prompt_activation',
}
payload=json.dumps(value,indent=2,sort_keys=True)+'\n'; path.write_text(payload); path.chmod(0o600)
checksum=Path(str(path)+'.sha256')
checksum.write_text(hashlib.sha256(payload.encode()).hexdigest()+'  '+str(path)+'\n')
checksum.chmod(0o600)
PY

phase=complete
printf '%s\n' 'memory_v1_pattern_salience_snapshot_production_apply_v5_1: PASS'
printf 'report=%s\nmanifest_sha256=%s\n' "$report" "$manifest_sha"
