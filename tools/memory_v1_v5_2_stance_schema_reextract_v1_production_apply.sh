#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs additive packet-recovery functions and appends
# exactly one reviewed re-extraction job. No claims, Qdrant, or prompt writes.

if [[ "${MEMORY_V1_V5_2_STANCE_SCHEMA_REEXTRACT_RUN:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_STANCE_SCHEMA_REEXTRACT_RUN=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-$repo_root/.env}"
set +a
python_bin="${MEMORY_V1_PYTHON:-$repo_root/venv/bin/python}"

container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_stance_schema_reextract_v1.lock
required_ancestor=f33d2584b52fcebc35897ea628c5ef1fe017b3d6
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=00a75a14-db47-50b7-bb19-40cfa046816c
content=70a0cef5245b571bc8fceaa4c744cc273b488bbacac08acb2718f831a2b920a1
storage=0db6a5a6c3681de0db804861ef639334e676599a25562fb11dce42fef8375d26
selector=20260722_v5_2_stance_schema_reextract_v1
plan_sha=364d0cad0553b99638b20b53131dbf6fe41307c77910aa275219c631ca880a0f
migration=ops/sql/20260722_memory_v1_v5_2_stance_schema_reextract_v1.sql
rollback=ops/sql/20260722_memory_v1_v5_2_stance_schema_reextract_v1_rollback.sql
test_sql=tests/memory_v1_v5_2_stance_schema_reextract_v1.sql
worker=scripts/memory_v1_v5_2_stance_schema_reextract_v1.py
provider=scripts/memory_v1_relational_extraction_v5_local_provider.py
provider_test=scripts/memory_v1_relational_extraction_v5_local_provider_test.py
clone_test=tools/memory_v1_v5_2_stance_schema_reextract_v1_clone.sh

declare -A expected_sha256=(
  ["$migration"]="d8731535902ba4660cb92a93bfb940787e3385ff83cb847b90eeb1bbed35c20e"
  ["$rollback"]="afccb892d3487c985f54bd57259b2ddf6af18b444cbb245c6dc097d86f7b376f"
  ["$test_sql"]="b0940f3e1529ecf9b6a47f8776d642cddecb18190444a3b0163ceccf68b820ee"
  ["$worker"]="eb119b9ec36a1c3604162ae4c5dd3957731c8eae4dc3c0cc98d432bfef8ee8ef"
  ["$provider"]="36f8f8bfa6fed1102f4ef5007fc33e0dcddc869aad624cfd7a94d3d7205db16a"
  ["$provider_test"]="2ccce6102afd2d1bfda3b5b19737faec1fb7d3260b52c0981dee1e90464114f5"
  ["$clone_test"]="2b6a1543f2f4a64abd2b7d40e1142ca5d163559c2e8afe64caa7e2e939ab8cfc"
)

timer_state=$(mktemp /tmp/memory-v5-2-stance-schema-reextract-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v5-2-stance-schema-reextract-tables.XXXXXX)
before=$(mktemp /tmp/memory-v5-2-stance-schema-reextract-before.XXXXXX)
after=$(mktemp /tmp/memory-v5-2-stance-schema-reextract-after.XXXXXX)
dry=$(mktemp /tmp/memory-v5-2-stance-schema-reextract-dry.XXXXXX)
applied=$(mktemp /tmp/memory-v5-2-stance-schema-reextract-applied.XXXXXX)
clone_output=$(mktemp /tmp/memory-v5-2-stance-schema-reextract-clone.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" "$dry" \
  "$applied" "$clone_output"
timers_quiesced=0
migration_installed=0
writes_committed=0
phase=initialization
status_file=

scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 -c "$1" | tr -d '[:space:]'
}

run_sql() {
  docker exec -i "$container" psql -U sage -d "$database" -X \
    -v ON_ERROR_STOP=1 "$@"
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$enabled" == enabled ]] && sudo -n systemctl enable "$unit" >/dev/null \
      || sudo -n systemctl disable "$unit" >/dev/null
    [[ "$active" == active ]] && sudo -n systemctl start "$unit" \
      || sudo -n systemctl stop "$unit"
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  rc=$?
  if [[ "$migration_installed" -eq 1 && "$writes_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || rc=1
  fi
  restore_timers || rc=1
  rm -f "$timer_state" "$table_list" "$before" "$after" "$dry" \
    "$applied" "$clone_output"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\nwrites_committed=%s\ncompleted_at=%s\n' \
      "$phase" "$rc" "$writes_committed" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$rc"
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

target_signature() {
  local table=$1
  scalar "SELECT count(*)::text || E'\\t' ||
    encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
      ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM (SELECT to_jsonb(value)::text AS row_json FROM memory.\"$table\" value
      WHERE selector_version IS DISTINCT FROM '$selector') rows"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for artifact in "${!expected_sha256[@]}"; do
  [[ -f "$artifact" ]]
  [[ "$(sha256sum "$artifact" | awk '{print $1}')" == \
    "${expected_sha256[$artifact]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
"$python_bin" -m py_compile "$worker"
PYTHONPATH="$repo_root" "$python_bin" "$provider_test" >/dev/null
[[ "$(systemctl --failed --no-legend --no-pager | wc -l)" -eq 0 ]]

phase=production_clone
bash "$clone_test" >"$clone_output"
[[ "$(tr -d '\r\n' <"$clone_output")" == \
  memory_v1_v5_2_stance_schema_reextract_v1_clone:\ PASS ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_stance_schema_reextract_v1_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_stance_schema_reextract_v1_${run_tag}.json"

phase=inventory_timers
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
timer_count=$(wc -l <"$timer_state")
(( timer_count >= 16 && timer_count <= 32 ))

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
partial="$snapshot_dir/.memory_pre_v5_v5_2_stance_schema_reextract_v1_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_v5_2_stance_schema_reextract_v1_${run_tag}.dump"
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
          ('evidence_extraction_job','evidence_intake_terminal',
           'evidence_extraction_event'))
      ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
jobs_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')
terminals_before=$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')
events_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')
jobs_existing_before=$(target_signature evidence_extraction_job)
terminals_existing_before=$(target_signature evidence_intake_terminal)
packet_before=$(scalar "SELECT encode(public.digest(convert_to(to_jsonb(value)::text,
  'UTF8'),'sha256'),'hex') FROM memory.evidence_extraction_packet_v5_local value
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")
qdrant_before=$(qdrant_signature)
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" == 0 ]]

phase=install_additive_functions
run_sql <"$migration" >/dev/null
migration_installed=1

phase=rollback_only_security
run_sql -v target_owner="$owner" -v other_owner="$other" \
  -v prior_packet="$packet" -v content_sha256="$content" \
  -v packet_storage_sha256="$storage" <"$test_sql" >/dev/null

phase=hash_locked_plan
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" "$python_bin" "$worker" \
  --owner-user-id "$owner" --prior-packet-id "$packet" \
  --expected-content-sha256 "$content" \
  --expected-packet-storage-sha256 "$storage" >"$dry"
jq -e --arg plan "$plan_sha" '
  .apply==false and .outcome=="eligible" and .plan_sha256==$plan and
  .write_counts=={"events":0,"jobs":0,"terminals":0} and
  .external_model_calls==0 and .claim_writes==0 and .qdrant_writes==0 and
  .prompt_influence==0
' "$dry" >/dev/null

phase=transactional_apply
MEMORY_V1_V5_2_STANCE_SCHEMA_REEXTRACT_APPLY=memory_v1_v5_2_stance_schema_reextract_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" "$python_bin" "$worker" \
  --owner-user-id "$owner" --prior-packet-id "$packet" \
  --expected-content-sha256 "$content" \
  --expected-packet-storage-sha256 "$storage" \
  --expected-plan-sha256 "$plan_sha" --apply >"$applied"
writes_committed=1
jq -e '
  .apply==true and .outcome=="queued" and
  .write_counts=={"events":1,"jobs":1,"terminals":1} and
  .zero_write_replay_proved==true and .external_model_calls==0 and
  .claim_writes==0 and .qdrant_writes==0 and .prompt_influence==0
' "$applied" >/dev/null

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')" \
  -eq "$((jobs_before+1))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')" \
  -eq "$((terminals_before+1))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" \
  -eq "$((events_before+1))" ]]
[[ "$(target_signature evidence_extraction_job)" == "$jobs_existing_before" ]]
[[ "$(target_signature evidence_intake_terminal)" == \
  "$terminals_existing_before" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND selector_version='$selector'
    AND status='pending' AND attempts=0")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$other'::uuid AND selector_version='$selector'")" == 0 ]]
[[ "$(scalar "SELECT encode(public.digest(convert_to(to_jsonb(value)::text,
  'UTF8'),'sha256'),'hex') FROM memory.evidence_extraction_packet_v5_local value
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")" == \
  "$packet_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

phase=restore_timers
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"

phase=report
dry_sha=$(sha256sum "$dry" | awk '{print $1}')
applied_sha=$(sha256sum "$applied" | awk '{print $1}')
jq -n --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" --arg dry_sha256 "$dry_sha" \
  --arg applied_sha256 "$applied_sha" --arg plan_sha256 "$plan_sha" \
  --arg qdrant_sha256 "$qdrant_after" --argjson timer_count "$timer_count" \
  '{contract_version:"memory_v1_v5_2_stance_schema_reextract_v1_apply_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    artifacts:{dry_sha256:$dry_sha256,applied_sha256:$applied_sha256},
    scope:{owners:1,jobs:1,terminals:1,events:1,external_model_calls:0,
      local_model_calls:0,claim_writes:0,qdrant_writes:0,prompt_influence:0},
    checks:{production_clone_passed:true,fresh_backup:true,
      hash_locked_artifacts:true,hash_locked_plan:true,
      all_discovered_memory_timers_quiesced:true,
      rollback_only_security_passed:true,transactional_apply:true,
      zero_write_replay:true,owner_isolation:true,
      original_packet_unchanged:true,all_non_target_rows_unchanged:true,
      qdrant_unchanged:true,exact_timer_states_restored:true},
    metrics:{discovered_timer_count:$timer_count},plan_sha256:$plan_sha256,
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_private_inference_on_reextract_job"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
jq -e '.checks | to_entries | map(.value==true) | all' "$report" >/dev/null
migration_installed=0
phase=complete
printf '%s\nreport=%s\nbackup=%s\n' \
  'memory_v1_v5_2_stance_schema_reextract_v1_production_apply: PASS' \
  "$report" "$backup"
