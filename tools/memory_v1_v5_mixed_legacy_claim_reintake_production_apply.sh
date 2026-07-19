#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Transactionally enqueues exactly one reviewed,
# owner-scoped mixed preference/legacy-claim evidence row for private V5
# re-extraction. It does not modify preferences, claims, Qdrant, or prompts.

if [[ "${MEMORY_V1_V5_MIXED_REINTAKE_APPLY_RUN:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_MIXED_REINTAKE_APPLY_RUN=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_mixed_reintake_apply.lock
required_ancestor=4a2033c21b3352b4185fc0e1f972cd88b0da38f9
owner_a=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
owner_b=557ea042-cb82-48f8-9429-472e96c957ef
owner_a_sha256=25f04f2cec24bcd3809a604da7eeeedcdb4e4fb0246c90d71315bd0ca4d65534
selector=20260719_v5_mixed_preference_legacy_reintake_v1
expected_plan_sha256=8fa0265df1db7230267a5109e9339f07b3b8fb07e4b0e0a113f77509a1272bda
worker=scripts/memory_v1_v5_mixed_legacy_claim_reintake.py
worker_test=scripts/memory_v1_v5_mixed_legacy_claim_reintake_test.py
rollback_test=tests/memory_v1_v5_mixed_legacy_claim_reintake.sql
postapply_test=tests/memory_v1_v5_mixed_legacy_claim_reintake_postapply.sql
migration=ops/sql/20260719_memory_v1_v5_mixed_legacy_claim_reintake.sql

declare -A expected_sha256=(
  ["$worker"]="3e8170aca8ad5ba2ecaf01351780f53c28b5d1797f095a2a9d4886f0f1587772"
  ["$worker_test"]="3d87dfc9000b3b7925306500682849ab9a14060171ddba0ffdc2d859cd83625f"
  ["$rollback_test"]="5bd71bb59bb9c11cf7101a7a1a4223c25b4c4deb27182cd18ea628814304935f"
  ["$postapply_test"]="af419d68b3aeefdcec5d2fcc521af6eb5725f2e95441ad0cb8f26d78ed7383ce"
  ["$migration"]="be78a2a441d96027fc54b092904c8e5d79f3025ad04759c034db0735b3c09987"
)

required_timers=(
  memory-v1-consolidation.timer
  memory-v1-deferred-reconciliation-scan.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-governance.timer
  memory-v1-projection.timer
  memory-v1-v5-chat-capture.timer
  memory-v1-v5-local-auto-resolution.timer
  memory-v1-v5-local-auto-stage.timer
  memory-v1-v5-local-claim-projection.timer
  memory-v1-v5-local-entailment.timer
  memory-v1-v5-local-entity-validation.timer
  memory-v1-v5-local-inference-health.timer
  memory-v1-v5-local-inference-scheduler.timer
  memory-v1-v5-local-legacy-reintake-audit.timer
  memory-v1-v5-local-legacy-reintake.timer
  memory-v1-v5-local-packet-router.timer
)

timer_state=$(mktemp /tmp/memory-v1-v5-mixed-apply-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-mixed-apply-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-mixed-apply-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-mixed-apply-after.XXXXXX)
apply_stdout=$(mktemp /tmp/memory-v1-v5-mixed-apply.XXXXXX.json)
replay_stdout=$(mktemp /tmp/memory-v1-v5-mixed-replay.XXXXXX.json)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" \
  "$apply_stdout" "$replay_stdout"
timers_quiesced=0
writes_committed=0
phase=initialization
status_file=

run_sql() {
  docker exec -i "$container" psql -U sage -d "$database" -X \
    -v ON_ERROR_STOP=1 "$@"
}

scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 -c "$1" | tr -d '[:space:]'
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
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

record_exit() {
  exit_code=$?
  restore_timers || exit_code=1
  rm -f "$timer_state" "$table_list" "$before" "$after" \
    "$apply_stdout" "$replay_stdout"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\nwrites_committed=%s\ncompleted_at=%s\n' \
      "$phase" "$exit_code" "$writes_committed" \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

capture_state() {
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

table_signature() {
  local table=$1 predicate=$2
  scalar "SELECT count(*)::text || E'\\t' ||
    encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
      ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM (SELECT to_jsonb(value)::text AS row_json
      FROM memory.\"$table\" AS value WHERE $predicate) rows"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for required in "${!expected_sha256[@]}"; do
  [[ -f "$required" ]]
  [[ "$(sha256sum "$required" | awk '{print $1}')" \
    == "${expected_sha256[$required]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
PYTHONPATH="$repo_root" venv/bin/python "$worker_test" >/dev/null
python3 -m py_compile "$worker"
[[ "$(systemctl --failed --no-legend --no-pager | wc -l)" -eq 0 ]]
[[ "$(scalar "SELECT to_regprocedure('memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(text,integer,uuid)') IS NOT NULL")" == t ]]
[[ "$(scalar "SELECT to_regprocedure('memory.enqueue_owner_v5_mixed_legacy_claim_reintake_v1(uuid,text,text,text,text)') IS NOT NULL")" == t ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_mixed_reintake_apply_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_mixed_reintake_apply_${run_tag}.json"
preflight_report="$snapshot_dir/memory_v1_v5_mixed_reintake_preflight_${run_tag}.json"
apply_report="$snapshot_dir/memory_v1_v5_mixed_reintake_worker_${run_tag}.json"
replay_report="$snapshot_dir/memory_v1_v5_mixed_reintake_replay_${run_tag}.json"

phase=inventory_timers
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
timer_count=$(wc -l <"$timer_state")
(( timer_count >= 16 && timer_count <= 32 ))
for unit in "${required_timers[@]}"; do
  grep -Fqx "$unit"$'\t'"$(systemctl is-enabled "$unit")"$'\t'"$(systemctl is-active "$unit")" \
    "$timer_state"
done

phase=quiesce_timers
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
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
partial="$snapshot_dir/.memory_pre_v5_mixed_reintake_apply_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_mixed_reintake_apply_${run_tag}.dump"
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

phase=rollback_only_security
run_sql -v owner_a="$owner_a" -v owner_b="$owner_b" <"$rollback_test" >/dev/null

phase=baseline
docker exec "$container" psql -U sage -d "$database" -X -At -F $'\t' \
  -c "SELECT table_schema,table_name FROM information_schema.tables
      WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
        AND NOT (table_schema='memory' AND table_name IN
          ('evidence_extraction_job','evidence_intake_terminal','evidence_extraction_event'))
      ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector'")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal WHERE selector_version='$selector'")" == 0 ]]
jobs_total_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')
terminals_total_before=$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')
events_total_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')
jobs_existing_before=$(table_signature evidence_extraction_job \
  "selector_version IS DISTINCT FROM '$selector'")
terminals_existing_before=$(table_signature evidence_intake_terminal \
  "selector_version IS DISTINCT FROM '$selector'")
events_existing_before=$(table_signature evidence_extraction_event \
  "NOT EXISTS (SELECT 1 FROM memory.evidence_extraction_job j WHERE j.owner_user_id=value.owner_user_id AND j.job_id=value.job_id AND j.selector_version='$selector')")

phase=hash_locked_preflight
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner_a" --limit 100 --report-path "$preflight_report" \
  >/dev/null
jq -e --arg owner_sha "$owner_a_sha256" --arg plan_sha "$expected_plan_sha256" '
  .mode=="read_only_dry_run" and (.owners|length)==1 and
  .owners[0].owner_user_id_sha256==$owner_sha and
  .owners[0].plan_count==1 and .owners[0].plan_sha256==$plan_sha and
  .owners[0].legacy_claim_count==5 and .owners[0].active_preference_count==2 and
  .totals.planned==1 and .totals.after==1 and
  .claim_writes==0 and .preference_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .local_model_calls==0 and .prompt_influence==0
' "$preflight_report" >/dev/null

phase=transactional_apply
MEMORY_V1_V5_MIXED_REINTAKE_APPLY=memory_v1_v5_mixed_legacy_claim_reintake_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner_a" --limit 100 --apply \
  --report-path "$apply_report" >"$apply_stdout"
writes_committed=1
jq -e '.apply==true and .owner_count==1 and .totals.planned==1 and
  .totals.legacy_claims==5 and .totals.active_preferences==2 and
  .totals.applied==1 and .totals.replayed==1 and .totals.after==0 and
  .preference_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .local_model_calls==0 and .prompt_influence==0' \
  "$apply_stdout" >/dev/null

phase=zero_write_replay
MEMORY_V1_V5_MIXED_REINTAKE_APPLY=memory_v1_v5_mixed_legacy_claim_reintake_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner_a" --limit 100 --apply \
  --report-path "$replay_report" >"$replay_stdout"
jq -e '.apply==true and .owner_count==1 and .totals.planned==0 and
  .totals.legacy_claims==0 and .totals.active_preferences==0 and
  .totals.applied==0 and .totals.replayed==0 and .totals.after==0 and
  .preference_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .local_model_calls==0 and .prompt_influence==0' \
  "$replay_stdout" >/dev/null

phase=postapply_security
run_sql -v owner_a="$owner_a" -v owner_b="$owner_b" <"$postapply_test" >/dev/null

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')" -eq "$((jobs_total_before+1))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')" -eq "$((terminals_total_before+1))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" -eq "$((events_total_before+1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector' AND owner_user_id='$owner_a'::uuid AND status='pending'")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector' AND owner_user_id='$owner_b'::uuid")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal WHERE selector_version='$selector' AND owner_user_id='$owner_a'::uuid")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event e JOIN memory.evidence_extraction_job j USING (owner_user_id,job_id) WHERE j.selector_version='$selector' AND j.owner_user_id='$owner_a'::uuid")" == 1 ]]
[[ "$(table_signature evidence_extraction_job "selector_version IS DISTINCT FROM '$selector'")" == "$jobs_existing_before" ]]
[[ "$(table_signature evidence_intake_terminal "selector_version IS DISTINCT FROM '$selector'")" == "$terminals_existing_before" ]]
[[ "$(table_signature evidence_extraction_event "NOT EXISTS (SELECT 1 FROM memory.evidence_extraction_job j WHERE j.owner_user_id=value.owner_user_id AND j.job_id=value.job_id AND j.selector_version='$selector')")" == "$events_existing_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

phase=restore_timers
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"

phase=report
preflight_sha=$(sha256sum "$preflight_report" | awk '{print $1}')
apply_sha=$(sha256sum "$apply_report" | awk '{print $1}')
replay_sha=$(sha256sum "$replay_report" | awk '{print $1}')
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg preflight_report "$preflight_report" --arg preflight_sha256 "$preflight_sha" \
  --arg apply_report "$apply_report" --arg apply_sha256 "$apply_sha" \
  --arg replay_report "$replay_report" --arg replay_sha256 "$replay_sha" \
  --arg plan_sha256 "$expected_plan_sha256" \
  --arg qdrant_sha256 "$qdrant_after" --argjson timer_count "$timer_count" \
  '{contract_version:"memory_v1_v5_mixed_legacy_claim_reintake_apply_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    worker_reports:{preflight:{path:$preflight_report,sha256:$preflight_sha256},
      apply:{path:$apply_report,sha256:$apply_sha256},
      replay:{path:$replay_report,sha256:$replay_sha256}},
    scope:{owners:1,planned:1,legacy_claims:5,active_preferences:2,
      applied:1,internal_replays:1,external_model_calls:0,local_model_calls:0,
      preference_writes:0,claim_writes:0,qdrant_writes:0,prompt_influence:0},
    checks:{fresh_backup:true,hash_locked_artifacts:true,
      hash_locked_exact_plan:true,rollback_only_security_test_passed:true,
      all_discovered_memory_timers_quiesced:true,transactional_apply:true,
      exact_one_row_per_target_table:true,zero_write_replay:true,
      owner_distribution_one_and_zero:true,cross_owner_rejection:true,
      preference_lane_unchanged:true,existing_target_rows_unchanged:true,
      all_non_target_rows_unchanged:true,qdrant_unchanged:true,
      exact_timer_states_restored:true},
    metrics:{discovered_timer_count:$timer_count},
    plan_sha256:$plan_sha256,qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_private_inference_on_mixed_candidate"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
jq -e '.checks | to_entries | map(.value == true) | all' "$report" >/dev/null

phase=complete
printf '%s\n' 'memory_v1_v5_mixed_legacy_claim_reintake_production_apply: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
