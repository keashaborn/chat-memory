#!/usr/bin/env bash
set -euo pipefail

# Server: seebx backend. Installs the V5.2 ambiguous-transcription review
# contract and appends exactly one owner-scoped terminal disposition.

if [[ "${MEMORY_V1_V5_2_REVIEW_DEFERRAL_RUN:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_REVIEW_DEFERRAL_RUN=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-$repo_root/.env}"
set +a

owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=7357397a-26a3-5d19-aee4-f7284509cf3f
evidence=2e0c5951-a38f-5ffd-89ac-1e9e4c4c6721
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_review_deferral.lock
required_ancestor=0da365234b88100c8171ce8799afc159be1f9bd5
migration=ops/sql/20260722_memory_v1_v5_2_ambiguous_review_deferral.sql
rollback=ops/sql/20260722_memory_v1_v5_2_ambiguous_review_deferral_rollback.sql
security_test=tests/memory_v1_v5_2_ambiguous_review_deferral.sql
stage_guard_test=tests/memory_v1_v5_2_ambiguous_review_deferral_stage_guard.sql
builder=scripts/memory_v1_v5_build_local_review_deferral.py
worker=scripts/memory_v1_v5_local_review_deferral.py
provider=scripts/memory_v1_relational_extraction_v5_local_provider.py
provider_test=tests/test_memory_v1_local_provider_v5_2.py
worker_test=tests/test_memory_v1_v5_2_local_review_deferral.py
clone_test=tools/memory_v1_v5_2_ambiguous_review_deferral_clone.sh
python_bin="${MEMORY_V1_PYTHON:-$repo_root/venv/bin/python}"

declare -A expected_sha256=(
  ["$migration"]="6aefe02f5b08fc87c92433a63122e3aa1c54242e295721353d5b414c03c114fb"
  ["$rollback"]="e4c58af898d8e36a131b431694443fdaae7f7367823ddc2230bfa33bb54a5476"
  ["$security_test"]="d4ebfca90de93af42cba8b9df03cf09592648ffc3c8637699103b1922fd877b0"
  ["$stage_guard_test"]="1e5f0fd1ceea7d57afce76768aae7bc9deea7d52a7924d12e2847d13f3eb679f"
  ["$builder"]="6c15677848e4ebb49d7d9151501f5344362a94ee76da150f5fc1a1b893f22b0f"
  ["$worker"]="c5bcef4d12270ef975b0599b52d2a6f95db6e120c83027e4ded26241d9bd99f5"
  ["$provider"]="ec14482abdf518c94d555a8373d9eff21012c799fa6cd0daf62f165e382c6ef1"
  ["$provider_test"]="b550e475e4f7b500d046ce625e7eacada90988d386c00fe6253b73e709c671f1"
  ["$worker_test"]="7b6ac952f553625489f61ac45662c6c54af949f0bef83014293508f4f5a781fe"
  ["$clone_test"]="4804b9020ffdcaafc741924c2fc5c5180da98d16eb6c336eefbf3bf1f2066980"
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
schema_installed=0
data_written=0
installation_committed=0
timer_state=$(mktemp /tmp/memory-v5-2-review-deferral-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v5-2-review-deferral-tables.XXXXXX)
before=$(mktemp /tmp/memory-v5-2-review-deferral-before.XXXXXX)
after=$(mktemp /tmp/memory-v5-2-review-deferral-after.XXXXXX)
dry=$(mktemp /tmp/memory-v5-2-review-deferral-dry.XXXXXX.json)
applied=$(mktemp /tmp/memory-v5-2-review-deferral-applied.XXXXXX.json)
replayed=$(mktemp /tmp/memory-v5-2-review-deferral-replayed.XXXXXX.json)
security_log=$(mktemp /tmp/memory-v5-2-review-deferral-security.XXXXXX.log)
stage_guard_log=$(mktemp /tmp/memory-v5-2-review-deferral-stage.XXXXXX.log)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" "$dry" \
  "$applied" "$replayed" "$security_log" "$stage_guard_log"

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

query_rows() {
  docker exec "$container" psql -X -A -t -F $'\t' -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
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

service_health() {
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  [[ "$(systemctl is-active brains.service)" == active ]]
  curl --fail --silent --show-error --max-time 15 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --show-error --max-time 15 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
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

record_exit() {
  exit_code=$?
  if [[ "$schema_installed" -eq 1 && "$data_written" -eq 0 \
        && "$installation_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || exit_code=1
  fi
  restore_timers || exit_code=1
  rm -f "$timer_state" "$table_list" "$before" "$after" "$dry" \
    "$applied" "$replayed" "$security_log" "$stage_guard_log"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'schema_installed=%s\n' "$schema_installed"
      printf 'data_written=%s\n' "$data_written"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

capture_state() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    [[ "$schema" =~ ^[a-z][a-z0-9_]*$ ]]
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

other_disposition_signature() {
  scalar "
    SELECT count(*)::text || encode(public.digest(convert_to(
      coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
      'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(value)::text AS row_json
      FROM memory.v5_local_packet_disposition AS value
      WHERE NOT (
        owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
      )
    ) AS rows
  "
}

for artifact in "${!expected_sha256[@]}"; do
  [[ -f "$artifact" ]]
  [[ "$(sha256sum "$artifact" | awk '{print $1}')" == \
    "${expected_sha256[$artifact]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
bash -n "$clone_test"
"$python_bin" -m py_compile "$provider" "$builder" "$worker"
PYTHONPATH="$repo_root" "$python_bin" -m unittest \
  tests.test_memory_v1_local_provider_v5_2 \
  tests.test_memory_v1_v5_2_local_review_deferral >/dev/null
service_health

phase=production_clone
"$clone_test" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_review_deferral_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_review_deferral_${run_tag}.json"
decision="$review_root/v5-2-ambiguous-review-deferral-${run_tag}.json"

phase=quiesce_timers
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
timer_count=$(wc -l <"$timer_state")
(( timer_count >= 16 && timer_count <= 40 ))
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
partial="$snapshot_dir/.memory_pre_v5_2_review_deferral_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_review_deferral_${run_tag}.dump"
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
query_rows "
  SELECT table_schema,table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE'
    AND table_schema IN ('memory','public')
    AND NOT (
      table_schema='memory' AND table_name='v5_local_packet_disposition'
    )
  ORDER BY table_schema,table_name
" >"$table_list"
[[ -s "$table_list" ]]
capture_state "$before"
target_before=$(scalar "SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")
[[ "$target_before" == 0 ]]
disposition_count_before=$(scalar \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')
other_dispositions_before=$(other_disposition_signature)
qdrant_before=$(qdrant_signature)

phase=install_schema
run_sql <"$migration" >/dev/null
schema_installed=1
packet_storage_sha256=$(scalar "SELECT packet_storage_sha256
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND evidence_id='$evidence'::uuid
    AND normalized_packet->>'contract_version'=
      'memory_v1_relational_extraction_v5_2'")
[[ "$packet_storage_sha256" =~ ^[0-9a-f]{64}$ ]]

phase=rollback_only_security
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" -v other_owner="$other" \
  -v packet_id="$packet" \
  -v packet_storage_sha256="$packet_storage_sha256" \
  <"$security_test" >"$security_log"
grep -q 'memory_v1_v5_2_ambiguous_review_deferral: PASS' "$security_log"
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")" == 0 ]]

phase=build_decision
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" "$python_bin" "$builder" \
  --owner-user-id "$owner" --packet-id "$packet" \
  --reviewer-ref user_authorized_2026-07-21 \
  --review-root "$review_root" --output "$decision" >/dev/null
[[ "$(stat -c '%a:%U:%G' "$decision")" == 600:ubuntu:ubuntu ]]
! grep -q 'mental illness\|concepts like hell' "$decision"

phase=dry_run
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" "$python_bin" "$worker" \
  --owner-user-id "$owner" --packet-id "$packet" \
  --decision "$decision" >"$dry"
jq -e '.apply==false and .outcome=="eligible" and
  .reason_code=="ambiguous_transcription" and
  .promotion_eligible==false and .write_counts.dispositions==0 and
  .write_counts.staging==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .zero_write_replay_proved==false and
  .external_model_calls==0' "$dry" >/dev/null

phase=transactional_apply
MEMORY_V1_V5_2_REVIEW_DEFERRAL_APPLY=memory_v1_v5_2_local_review_deferral_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" "$python_bin" "$worker" \
  --owner-user-id "$owner" --packet-id "$packet" \
  --decision "$decision" --apply >"$applied"
if [[ "$(scalar "SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND evidence_id='$evidence'::uuid
    AND disposition='terminal_no_stage'
    AND review_decision='deferred'
    AND reason_code='ambiguous_transcription'
    AND NOT promotion_eligible")" == 1 ]]; then
  data_written=1
fi
[[ "$data_written" -eq 1 ]]
jq -e '.apply==true and .outcome=="deferred" and
  .write_counts.dispositions==1 and .write_counts.staging==0 and
  .write_counts.claims==0 and .write_counts.qdrant==0 and
  .zero_write_replay_proved==true and .external_model_calls==0' \
  "$applied" >/dev/null

phase=external_replay
MEMORY_V1_V5_2_REVIEW_DEFERRAL_APPLY=memory_v1_v5_2_local_review_deferral_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" "$python_bin" "$worker" \
  --owner-user-id "$owner" --packet-id "$packet" \
  --decision "$decision" --apply >"$replayed"
jq -e '.apply==true and .outcome=="deferred" and
  .write_counts.dispositions==0 and .write_counts.staging==0 and
  .write_counts.claims==0 and .write_counts.qdrant==0 and
  .zero_write_replay_proved==true and .external_model_calls==0' \
  "$replayed" >/dev/null

phase=stage_guard_security
run_sql -v target_owner="$owner" -v evidence_id="$evidence" \
  <"$stage_guard_test" >"$stage_guard_log"
grep -q 'memory_v1_v5_2_ambiguous_review_deferral_stage_guard: PASS' \
  "$stage_guard_log"

phase=postflight
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" \
  -eq "$((disposition_count_before+1))" ]]
[[ "$(other_disposition_signature)" == "$other_dispositions_before" ]]
[[ "$(scalar "SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid")" == 0 ]]
capture_state "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

phase=restore_timers
restore_timers
service_health
installation_committed=1

phase=report
decision_sha=$(sha256sum "$decision" | awk '{print $1}')
dry_sha=$(sha256sum "$dry" | awk '{print $1}')
applied_sha=$(sha256sum "$applied" | awk '{print $1}')
replayed_sha=$(sha256sum "$replayed" | awk '{print $1}')
jq -n --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg decision "$decision" --arg decision_sha256 "$decision_sha" \
  --arg dry_sha256 "$dry_sha" --arg applied_sha256 "$applied_sha" \
  --arg replayed_sha256 "$replayed_sha" --arg qdrant_sha256 "$qdrant_after" \
  --argjson timer_count "$timer_count" \
  '{contract_version:"memory_v1_v5_2_review_deferral_apply_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    artifacts:{decision_path:$decision,decision_sha256:$decision_sha256,
      dry_sha256:$dry_sha256,applied_sha256:$applied_sha256,
      replayed_sha256:$replayed_sha256},
    result:{dispositions_written:1,staging_rows_written:0,claims_written:0,
      qdrant_writes:0,prompt_influence:0,external_model_calls:0},
    checks:{production_clone_passed:true,fresh_backup:true,
      hash_locked_inputs:true,rollback_only_security_passed:true,
      append_only_disposition:true,zero_write_replay:true,
      stage_guard_enforced:true,owner_isolation:true,
      non_target_rows_unchanged:true,qdrant_unchanged:true,
      exact_timer_states_restored:true,service_health:true},
    metrics:{discovered_timer_count:$timer_count},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_reextraction_staging_claims_retrieval_or_prompt_influence"}' \
  >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
jq -e '.checks | to_entries | map(.value==true) | all' "$report" >/dev/null
phase=complete
printf '%s\nreport=%s\nbackup=%s\n' \
  'memory_v1_v5_2_ambiguous_review_deferral_production_apply: PASS' \
  "$report" "$backup"
