#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Replaces two controlled V5 relational staging functions
# so legacy non-UUID evidence external IDs use the canonical evidence UUID.
# The migration changes no rows and this installer never stages a packet.

if [[ "${MEMORY_V1_V5_STAGE_SOURCE_ID_COMPAT_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_STAGE_SOURCE_ID_COMPAT_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
foundation_commit=2f8e132f
migration=ops/sql/20260718_memory_v1_v5_stage_source_id_compat.sql
rollback=ops/sql/20260718_memory_v1_v5_stage_source_id_compat_rollback.sql
test_sql=tests/memory_v1_v5_stage_source_id_compat.sql
expected_migration_sha=a45ec57f3c5ecb0838fb019d4d485d25b34110688cc6d4bb25cbea32b17a9383
expected_rollback_sha=2a59e06a107b9646d9f2ef2bf94274581ccac7f5a115f5bf53b47d522e016aa4
expected_test_sha=e06b6dbe4d8d4559c7669004d3af9668eeb47e52324c96536b1f53c0def79816
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
evidence_id=00614247-c79d-5c63-9c37-d53419118b06
source_id=00614247-c79d-5c63-9c37-d53419118b06
source_sha256=6886231333bed756f3643f81acecf3644c69b5e3f6ffc07f6b8edbaadf83f3c7
source_recorded_at=2026-07-13T21:38:29.305701Z
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_stage_source_id_compat_install.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_state=$(mktemp /tmp/memory-v1-v5-stage-source-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-stage-source-tables.XXXXXX)

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

run_sql_file() {
  local file=$1
  shift
  docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=180s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
    "$@" <"$repo_root/$file"
}

restore_timers() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

record_exit() {
  exit_code=$?
  restore_timers || exit_code=1
  rm -f "$unit_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_state() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

for file in "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$file" ]]
done
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor \
  "$foundation_commit" "$(git -C "$repo_root" rev-parse HEAD)"
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_stage_source_id_compat_install_${run_tag}.status"
before="$snapshot_dir/memory_v1_v5_stage_source_id_compat_before_${run_tag}.tsv"
after="$snapshot_dir/memory_v1_v5_stage_source_id_compat_after_${run_tag}.tsv"
log="$snapshot_dir/memory_v1_v5_stage_source_id_compat_install_${run_tag}.log"

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regprocedure(
    'memory.preflight_relational_stage_bundle_v5(uuid,text,text,timestamptz)'
  ) IS NOT NULL
  AND to_regprocedure(
    'memory.stage_relational_packet_v5(uuid,uuid,uuid,uuid,text,text,text,timestamptz,jsonb,jsonb,jsonb,jsonb,jsonb,text,text,text)'
  ) IS NOT NULL
  AND NOT has_table_privilege(
    'brains_app','memory.evidence','SELECT'
  )
)::integer")" == 1 ]]

: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$unit_state" ]]
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$unit_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_stage_source_id_compat_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_stage_source_id_compat_${run_tag}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
psql_scalar "SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)

phase=install
run_sql_file "$migration" >"$log" 2>&1

phase=rollback_only_security_test
run_sql_file "$test_sql" \
  -v target_owner="$target_owner" \
  -v other_owner="$other_owner" \
  -v evidence_id="$evidence_id" \
  -v source_id="$source_id" \
  -v source_sha256="$source_sha256" \
  -v source_recorded_at="$source_recorded_at" >>"$log" 2>&1
grep -q 'memory_v1_v5_stage_source_id_compat: PASS' "$log"
chmod 0600 "$log"

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$(psql_scalar "SELECT (
  pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid=
    'memory.preflight_relational_stage_bundle_v5(uuid,text,text,timestamptz)'::regprocedure
  ))='memory_v5_writer'
  AND pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid=
    'memory.stage_relational_packet_v5(uuid,uuid,uuid,uuid,text,text,text,timestamptz,jsonb,jsonb,jsonb,jsonb,jsonb,text,text,text)'::regprocedure
  ))='memory_v5_writer'
  AND has_function_privilege(
    'brains_app','memory.preflight_relational_stage_bundle_v5(uuid,text,text,timestamptz)','EXECUTE'
  )
  AND NOT has_table_privilege('brains_app','memory.evidence','SELECT')
)::integer")" == 1 ]]

restore_timers

phase=report
report="$snapshot_dir/memory_v1_v5_stage_source_id_compat_install_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg before "$before" --arg after "$after" --arg log "$log" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_stage_source_id_compat_install_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    evidence:{before:$before,after:$after,log:$log,qdrant_sha256:$qdrant_sha256},
    checks:{fresh_backup:true,function_compatibility_only:true,
      owner_scoped_preflight:true,cross_owner_rejection:true,
      application_has_no_direct_evidence_select:true,
      rollback_only_security_test:true,memory_rows_unchanged:true,
      qdrant_unchanged:true,timers_restored:true,external_model_calls:0},
    hard_stop:"before_relational_staging_apply_or_entity_resolution_or_projection_or_live_retrieval"}' \
