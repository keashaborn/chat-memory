#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Transactionally enqueues exactly five reviewed,
# owner-scoped legacy-only evidence rows for private V5 re-extraction.

if [[ "${MEMORY_V1_V5_LEGACY_REINTAKE_APPLY_RUN:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LEGACY_REINTAKE_APPLY_RUN=authorized is required' >&2
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
lock_file=/home/ubuntu/brains/.memory_v1_v5_legacy_reintake_apply.lock
required_ancestor=d921f9a56a05dc356e42f20fa2bc2eb6d3bfdafc
owner_a=557ea042-cb82-48f8-9429-472e96c957ef
owner_b=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
selector=20260719_v5_legacy_claim_reintake_v1
worker=scripts/memory_v1_v5_legacy_claim_reintake.py
worker_test=scripts/memory_v1_v5_legacy_claim_reintake_test.py
postapply_test=tests/memory_v1_v5_legacy_claim_reintake_postapply.sql
migration=ops/sql/20260719_memory_v1_v5_legacy_claim_reintake.sql

declare -A expected_sha256=(
  ["$worker"]="cc27ec9d74ce7c825e842ef2900416f1e48aebf9cd3f3808bd9062d52a1ec278"
  ["$worker_test"]="b254b22ee9e7d0dedb161a50e76b48c85c7619a0e1095d088fb8aa25f6074e5e"
  ["$postapply_test"]="8375a89015f27531680972e32b99a8415be4249846eb1d9af8a0c56835ffc7fb"
  ["$migration"]="b197fa613316e66a63dccc147cd695d7715235fa67c70ad593c9a0530d92af88"
)

timer_state=$(mktemp /tmp/memory-v1-v5-legacy-apply-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-legacy-apply-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-legacy-apply-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-legacy-apply-after.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$before" "$after"
timers_quiesced=0
writes_committed=0
phase=initialization
status_file=
apply_stdout=
replay_stdout=

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
  rm -f "$timer_state" "$table_list" "$before" "$after"
  [[ -z "$apply_stdout" ]] || rm -f "$apply_stdout"
  [[ -z "$replay_stdout" ]] || rm -f "$replay_stdout"
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

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_legacy_reintake_apply_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_legacy_reintake_apply_${run_tag}.json"
apply_report="$snapshot_dir/memory_v1_v5_legacy_reintake_worker_${run_tag}.json"
replay_report="$snapshot_dir/memory_v1_v5_legacy_reintake_replay_${run_tag}.json"
apply_stdout=$(mktemp /tmp/memory-v1-v5-legacy-apply.XXXXXX.json)
replay_stdout=$(mktemp /tmp/memory-v1-v5-legacy-replay.XXXXXX.json)
chmod 0600 "$apply_stdout" "$replay_stdout"

phase=quiesce_timers
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$timer_state")" -eq 13 ]]
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
partial="$snapshot_dir/.memory_pre_v5_legacy_reintake_apply_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_legacy_reintake_apply_${run_tag}.dump"
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
events_existing_before=$(table_signature evidence_extraction_event true)

phase=transactional_apply
MEMORY_V1_V5_LEGACY_REINTAKE_APPLY=memory_v1_v5_legacy_claim_reintake_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner_a" --owner-user-id "$owner_b" --limit 100 --apply \
  --report-path "$apply_report" >"$apply_stdout"
writes_committed=1
jq -e '.apply==true and .owner_count==2 and .totals.planned==5 and
  .totals.applied==5 and .totals.replayed==5 and .totals.after==0 and
  .claim_writes==0 and .qdrant_writes==0 and .external_model_calls==0 and
  .local_model_calls==0 and .prompt_influence==0' "$apply_stdout" >/dev/null

phase=zero_write_replay
MEMORY_V1_V5_LEGACY_REINTAKE_APPLY=memory_v1_v5_legacy_claim_reintake_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner_a" --owner-user-id "$owner_b" --limit 100 --apply \
  --report-path "$replay_report" >"$replay_stdout"
jq -e '.apply==true and .owner_count==2 and .totals.planned==0 and
  .totals.applied==0 and .totals.replayed==0 and .totals.after==0 and
  .claim_writes==0 and .qdrant_writes==0 and .external_model_calls==0 and
  .local_model_calls==0 and .prompt_influence==0' "$replay_stdout" >/dev/null

phase=postapply_security
run_sql -v owner_a="$owner_a" -v owner_b="$owner_b" <"$postapply_test" >/dev/null

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')" -eq "$((jobs_total_before+5))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')" -eq "$((terminals_total_before+5))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" -eq "$((events_total_before+5))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector' AND owner_user_id='$owner_a'::uuid")" == 4 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector' AND owner_user_id='$owner_b'::uuid")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal WHERE selector_version='$selector'")" == 5 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event e JOIN memory.evidence_extraction_job j USING (owner_user_id,job_id) WHERE j.selector_version='$selector'")" == 5 ]]
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
apply_sha=$(sha256sum "$apply_report" | awk '{print $1}')
replay_sha=$(sha256sum "$replay_report" | awk '{print $1}')
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg apply_report "$apply_report" --arg apply_sha256 "$apply_sha" \
  --arg replay_report "$replay_report" --arg replay_sha256 "$replay_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_legacy_claim_reintake_apply_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    worker_reports:{apply:{path:$apply_report,sha256:$apply_sha256},
      replay:{path:$replay_report,sha256:$replay_sha256}},
    scope:{owners:2,planned:5,applied:5,internal_replays:5,
      external_model_calls:0,local_model_calls:0,claim_writes:0,
      qdrant_writes:0,prompt_influence:0},
    checks:{fresh_backup:true,hash_locked_artifacts:true,
      all_memory_timers_quiesced:true,transactional_apply:true,
      exact_five_rows_per_target_table:true,zero_write_replay:true,
      owner_distribution_four_and_one:true,cross_owner_rejection:true,
      existing_target_rows_unchanged:true,all_non_target_rows_unchanged:true,
      qdrant_unchanged:true,exact_timer_states_restored:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_manual_private_inference_verification"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
jq -e '.checks | to_entries | map(.value == true) | all' "$report" >/dev/null
rm -f "$apply_stdout" "$replay_stdout"

phase=complete
printf '%s\n' 'memory_v1_v5_legacy_claim_reintake_production_apply: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
