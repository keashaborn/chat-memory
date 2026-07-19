#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Recovers the bounded apply proof after the original
# post-apply assertion used an incorrect owner distribution. Writes no rows.

if [[ "${MEMORY_V1_V5_LEGACY_REINTAKE_POSTVERIFY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LEGACY_REINTAKE_POSTVERIFY=authorized is required' >&2
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
lock_file=/home/ubuntu/brains/.memory_v1_v5_legacy_reintake_postverify.lock
required_ancestor=ad65922f06244562b722e0419cd7e701a4a99252
owner_a=557ea042-cb82-48f8-9429-472e96c957ef
owner_b=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
selector=20260719_v5_legacy_claim_reintake_v1
worker=scripts/memory_v1_v5_legacy_claim_reintake.py
worker_test=scripts/memory_v1_v5_legacy_claim_reintake_test.py
postapply_test=tests/memory_v1_v5_legacy_claim_reintake_postapply.sql

declare -A expected_sha256=(
  ["$worker"]="cc27ec9d74ce7c825e842ef2900416f1e48aebf9cd3f3808bd9062d52a1ec278"
  ["$worker_test"]="b254b22ee9e7d0dedb161a50e76b48c85c7619a0e1095d088fb8aa25f6074e5e"
  ["$postapply_test"]="4bfb29487a23a7cc9e919de81c8a4977092b245023e9e5c9d32f1a50b99ea216"
)

timer_state=$(mktemp /tmp/memory-v1-v5-legacy-postverify-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-legacy-postverify-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-legacy-postverify-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-legacy-postverify-after.XXXXXX)
replay_stdout=$(mktemp /tmp/memory-v1-v5-legacy-postverify-replay.XXXXXX.json)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" "$replay_stdout"
timers_quiesced=0
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
  rm -f "$timer_state" "$table_list" "$before" "$after" "$replay_stdout"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$phase" "$exit_code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
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

original_apply_report=$(ls -1t \
  "$snapshot_dir"/memory_v1_v5_legacy_reintake_worker_*.json | head -1)
original_replay_report=$(ls -1t \
  "$snapshot_dir"/memory_v1_v5_legacy_reintake_replay_*.json | head -1)
jq -e '.totals=={"after":0,"applied":5,"planned":5,"replayed":5} and
  .claim_writes==0 and .qdrant_writes==0 and .external_model_calls==0 and
  .local_model_calls==0 and .prompt_influence==0' "$original_apply_report" >/dev/null
jq -e '.totals=={"after":0,"applied":0,"planned":0,"replayed":0} and
  .claim_writes==0 and .qdrant_writes==0 and .external_model_calls==0 and
  .local_model_calls==0 and .prompt_influence==0' "$original_replay_report" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_legacy_reintake_postverify_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_legacy_reintake_postverify_${run_tag}.json"
replay_report="$snapshot_dir/memory_v1_v5_legacy_reintake_postverify_replay_${run_tag}.json"

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
partial="$snapshot_dir/.memory_post_v5_legacy_reintake_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_post_v5_legacy_reintake_${run_tag}.dump"
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

phase=exact_applied_set
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector'")" == 5 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector' AND owner_user_id='$owner_a'::uuid")" == 5 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector' AND owner_user_id='$owner_b'::uuid")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector' AND status='pending'")" == 5 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal WHERE selector_version='$selector'")" == 5 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event e JOIN memory.evidence_extraction_job j USING (owner_user_id,job_id) WHERE j.selector_version='$selector' AND e.event_type='queued' AND e.actor_ref='v5_legacy_claim_reintake'")" == 5 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job j JOIN memory.evidence e USING (owner_user_id,evidence_id) WHERE j.selector_version='$selector' AND j.evidence_content_sha256=e.content_sha256 AND EXISTS (SELECT 1 FROM memory.claim_evidence l JOIN memory.claim c ON c.owner_user_id=l.owner_user_id AND c.claim_id=l.claim_id JOIN memory.predicate_contract pc ON pc.registry_version='memory_predicate_registry_v5' AND pc.predicate=c.predicate WHERE l.owner_user_id=j.owner_user_id AND l.evidence_id=j.evidence_id AND pc.lifecycle='legacy_read_only') AND NOT EXISTS (SELECT 1 FROM memory.preference_revision_evidence p WHERE p.owner_user_id=j.owner_user_id AND p.evidence_id=j.evidence_id) AND NOT EXISTS (SELECT 1 FROM memory.project_knowledge_revision_evidence p WHERE p.owner_user_id=j.owner_user_id AND p.evidence_id=j.evidence_id) AND NOT EXISTS (SELECT 1 FROM memory.relational_stage_batch s WHERE s.owner_user_id=j.owner_user_id AND s.evidence_id=j.evidence_id) AND NOT EXISTS (SELECT 1 FROM memory.observation_entailment_v5 o WHERE o.owner_user_id=j.owner_user_id AND o.evidence_id=j.evidence_id)")" == 5 ]]

phase=baseline
docker exec "$container" psql -U sage -d "$database" -X -At -F $'\t' \
  -c "SELECT table_schema,table_name FROM information_schema.tables
      WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
      ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)

phase=zero_write_replay
MEMORY_V1_V5_LEGACY_REINTAKE_APPLY=memory_v1_v5_legacy_claim_reintake_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner_a" --owner-user-id "$owner_b" --limit 100 --apply \
  --report-path "$replay_report" >"$replay_stdout"
jq -e '.apply==true and .owner_count==2 and .totals.planned==0 and
  .totals.applied==0 and .totals.replayed==0 and .totals.after==0 and
  .claim_writes==0 and .qdrant_writes==0 and .external_model_calls==0 and
  .local_model_calls==0 and .prompt_influence==0' "$replay_stdout" >/dev/null

phase=account_isolation
run_sql -v owner_a="$owner_a" -v owner_b="$owner_b" <"$postapply_test" >/dev/null

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

phase=restore_timers
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"

phase=report
original_apply_sha=$(sha256sum "$original_apply_report" | awk '{print $1}')
original_replay_sha=$(sha256sum "$original_replay_report" | awk '{print $1}')
recovery_replay_sha=$(sha256sum "$replay_report" | awk '{print $1}')
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg original_apply "$original_apply_report" \
  --arg original_apply_sha256 "$original_apply_sha" \
  --arg original_replay "$original_replay_report" \
  --arg original_replay_sha256 "$original_replay_sha" \
  --arg recovery_replay "$replay_report" \
  --arg recovery_replay_sha256 "$recovery_replay_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_legacy_claim_reintake_postverify_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    reports:{original_apply:{path:$original_apply,sha256:$original_apply_sha256},
      original_replay:{path:$original_replay,sha256:$original_replay_sha256},
      recovery_replay:{path:$recovery_replay,sha256:$recovery_replay_sha256}},
    scope:{owners:2,applied:5,owner_a:5,owner_b:0,
      external_model_calls:0,local_model_calls:0,claim_writes:0,
      qdrant_writes:0,prompt_influence:0},
    checks:{fresh_postapply_backup:true,hash_locked_artifacts:true,
      exact_applied_set:true,legacy_only_relational_eligibility:true,
      preference_lane_excluded:true,zero_write_replay:true,
      owner_distribution_five_and_zero:true,cross_owner_rejection:true,
      all_rows_unchanged_during_recovery:true,qdrant_unchanged:true,
      exact_timer_states_restored:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_private_inference_cycle"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
jq -e '.checks | to_entries | map(.value == true) | all' "$report" >/dev/null

phase=complete
printf '%s\n' 'memory_v1_v5_legacy_claim_reintake_postapply_verify: PASS'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
