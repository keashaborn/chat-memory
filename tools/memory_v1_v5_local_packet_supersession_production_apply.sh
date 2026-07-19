#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the additive supersession ledger and appends
# exactly one owner-scoped V5->V6 packet supersession. No model, claim,
# Qdrant, projection, retrieval, or prompt writes.

if [[ "${MEMORY_V1_V5_LOCAL_PACKET_SUPERSESSION_RUN:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LOCAL_PACKET_SUPERSESSION_RUN=authorized is required' >&2
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
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_packet_supersession.lock
required_ancestor=6a81421ae7a31e75f88fbfa4ce80543bccdcca32
owner=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
other=557ea042-cb82-48f8-9429-472e96c957ef
prior=5455ceae-ae9d-5832-a491-18f4fee41284
replacement=72b091a6-22be-549e-9c63-a1a4360f3eac
prior_storage=e1725c0c94fbaeff146fe6959b6084729495c59eb7c79d1c5feed9a984a6fa1a
replacement_storage=7d57a27543cbc93335cdc31ed3e7da3cdfc828adbcf1c427acbdbbf55758dfc5
plan_sha=42749cf4f464ff71525710a3c068ddbea4698bc79b41786d5d24fdba70db7bff
migration=ops/sql/20260719_memory_v1_v5_local_packet_supersession.sql
rollback=ops/sql/20260719_memory_v1_v5_local_packet_supersession_rollback.sql
test_sql=tests/memory_v1_v5_local_packet_supersession.sql
worker=scripts/memory_v1_v5_local_packet_supersession.py
clone_test=tools/memory_v1_v5_local_packet_supersession_clone.sh

declare -A expected_sha256=(
  ["$migration"]="b89e590b804c08c2c4f89bb1e53343a0b26f19ca2c253f24e2451fa1e616fe81"
  ["$rollback"]="e7ae4dde82592d4ddfdd04ea1f1272f41eb5c0bed6f4aa9639bbe8ab56fcf88d"
  ["$test_sql"]="ccfe835a8b295efc75b3a54928b131ed20411813a397956b8f8218178204d791"
  ["$worker"]="2800e4ac0249fbdd92bdb2e9c370916dfa7b65df0a858cd43f7238b6d50fef8d"
  ["$clone_test"]="743218e873cc919811de16a993d90f56a0e26c0b3ab9e9e6cd340a0db3afdf57"
)

timer_state=$(mktemp /tmp/memory-v5-packet-supersession-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v5-packet-supersession-tables.XXXXXX)
before=$(mktemp /tmp/memory-v5-packet-supersession-before.XXXXXX)
after=$(mktemp /tmp/memory-v5-packet-supersession-after.XXXXXX)
dry=$(mktemp /tmp/memory-v5-packet-supersession-dry.XXXXXX)
applied=$(mktemp /tmp/memory-v5-packet-supersession-applied.XXXXXX)
clone_output=$(mktemp /tmp/memory-v5-packet-supersession-clone.XXXXXX)
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

packet_signature() {
  local packet=$1
  scalar "SELECT encode(public.digest(convert_to(to_jsonb(value)::text,
    'UTF8'),'sha256'),'hex')
    FROM memory.evidence_extraction_packet_v5_local value
    WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid"
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
python3 -m py_compile "$worker"
[[ "$(systemctl --failed --no-legend --no-pager | wc -l)" -eq 0 ]]

phase=production_clone
bash "$clone_test" >"$clone_output"
[[ "$(tr -d '\r\n' <"$clone_output")" == \
  memory_v1_v5_local_packet_supersession_clone:\ PASS ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_local_packet_supersession_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_local_packet_supersession_${run_tag}.json"

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
partial="$snapshot_dir/.memory_pre_v5_packet_supersession_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_packet_supersession_${run_tag}.dump"
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
      ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
prior_before=$(packet_signature "$prior")
replacement_before=$(packet_signature "$replacement")
qdrant_before=$(qdrant_signature)
[[ "$(scalar "SELECT to_regclass(
  'memory.v5_local_packet_supersession') IS NULL")" == t ]]

phase=install_additive_schema
run_sql <"$migration" >/dev/null
migration_installed=1

phase=rollback_only_security
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" -v other_owner="$other" \
  -v prior_packet="$prior" -v replacement_packet="$replacement" \
  -v prior_storage_sha256="$prior_storage" \
  -v replacement_storage_sha256="$replacement_storage" \
  -v operation_id=00000000-0000-4000-8000-000000000201 \
  -v supersession_id=00000000-0000-4000-8000-000000000202 \
  <"$test_sql" >/dev/null

phase=hash_locked_plan
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner" --prior-packet-id "$prior" \
  --replacement-packet-id "$replacement" \
  --expected-prior-storage-sha256 "$prior_storage" \
  --expected-replacement-storage-sha256 "$replacement_storage" >"$dry"
jq -e --arg plan "$plan_sha" '
  .apply==false and .outcome=="eligible" and .plan_sha256==$plan and
  .write_counts=={"supersessions":0} and .external_model_calls==0 and
  .claim_writes==0 and .qdrant_writes==0 and .prompt_influence==0
' "$dry" >/dev/null

phase=transactional_apply
MEMORY_V1_V5_LOCAL_PACKET_SUPERSESSION_APPLY=memory_v1_v5_local_packet_supersession_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner" --prior-packet-id "$prior" \
  --replacement-packet-id "$replacement" \
  --expected-prior-storage-sha256 "$prior_storage" \
  --expected-replacement-storage-sha256 "$replacement_storage" \
  --expected-plan-sha256 "$plan_sha" --apply >"$applied"
writes_committed=1
jq -e '
  .apply==true and .outcome=="superseded" and
  .write_counts=={"supersessions":1} and
  .zero_write_replay_proved==true and .external_model_calls==0 and
  .claim_writes==0 and .qdrant_writes==0 and .prompt_influence==0
' "$applied" >/dev/null

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_supersession
  WHERE owner_user_id='$owner'::uuid AND prior_packet_id='$prior'::uuid
    AND replacement_packet_id='$replacement'::uuid")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_supersession
  WHERE owner_user_id='$other'::uuid")" == 0 ]]
[[ "$(packet_signature "$prior")" == "$prior_before" ]]
[[ "$(packet_signature "$replacement")" == "$replacement_before" ]]
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
  '{contract_version:"memory_v1_v5_local_packet_supersession_apply_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    artifacts:{dry_sha256:$dry_sha256,applied_sha256:$applied_sha256},
    scope:{owners:1,supersessions:1,external_model_calls:0,
      local_model_calls:0,claim_writes:0,qdrant_writes:0,
      prompt_influence:0},
    checks:{production_clone_passed:true,fresh_backup:true,
      hash_locked_artifacts:true,hash_locked_plan:true,
      all_discovered_memory_timers_quiesced:true,
      rollback_only_security_passed:true,transactional_apply:true,
      zero_write_replay:true,owner_isolation:true,
      prior_packet_unchanged:true,replacement_packet_unchanged:true,
      all_preexisting_rows_unchanged:true,qdrant_unchanged:true,
      exact_timer_states_restored:true},
    metrics:{discovered_timer_count:$timer_count},plan_sha256:$plan_sha256,
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_corrected_packet_review_routing"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
jq -e '.checks | to_entries | map(.value==true) | all' "$report" >/dev/null
migration_installed=0
phase=complete
printf '%s\nreport=%s\nbackup=%s\n' \
  'memory_v1_v5_local_packet_supersession_production_apply: PASS' \
  "$report" "$backup"
