#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_V5_2_EMPLOYMENT_COMPILER_V6_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_EMPLOYMENT_COMPILER_V6_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260723_memory_v1_v5_2_employment_compiler_v6.sql
rollback=ops/sql/20260723_memory_v1_v5_2_employment_compiler_v6_rollback.sql
test_sql=tests/memory_v1_v5_2_employment_compiler_v6.sql
manifest=ops/manifests/memory_v1_v5_2_employment_compiler_v6_20260723.json
expected_migration_sha=52b8cf370be9c078ccc5d8440a4a65aa6777e942ede1e492d3e454023789c645
expected_rollback_sha=38860994c1f5a5430367736355998cb166a415a920214114f628897eb528a4f3
expected_test_sha=c158ea238238d8d69ae8e80d551ddb4191581f0febd950e8591017ce1d86b297
expected_manifest_sha=ce37f406baf25fb5fd3e0b6e023df6370fa7e7f5b3c628ac2e79d55f19b76910
required_ancestor=9063183fb6262a2d2135fd9a95a56d5a367a9db5
expected_function_sha=39715a76bec7dceebbe1b15e25d2fff8300d42331a8bfb6c557070fa4f3e53ae
container=brains-postgres-1
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_employment_compiler_v6.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_state=$(mktemp /tmp/memory-v1-v5-2-employment-v6-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-employment-v6-tables.XXXXXX)

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
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

capture_memory_state() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "SELECT count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
        ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

for required in "$migration" "$rollback" "$test_sql" "$manifest"; do
  [[ -f "$repo_root/$required" ]]
done
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | cut -d' ' -f1)" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$expected_test_sha" ]]
[[ "$(sha256sum "$repo_root/$manifest" | cut -d' ' -f1)" == "$expected_manifest_sha" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
set -a
source /opt/chat-memory/.env
set +a
[[ "$(psql "$POSTGRES_DSN" -X -A -t -v ON_ERROR_STOP=1 \
  -c 'SELECT session_user')" == brains_app ]]

run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_employment_v6_${run_tag}.status"
before="$snapshot_dir/memory_v1_v5_2_employment_v6_before_${run_tag}.tsv"
after="$snapshot_dir/memory_v1_v5_2_employment_v6_after_${run_tag}.tsv"

phase=quiesce
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
backup_partial="$snapshot_dir/.memory_pre_v5_2_employment_v6_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_employment_v6_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
psql_scalar "SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name" >"$table_list"
capture_memory_state "$before"
qdrant_before=$(qdrant_signature)

phase=install_and_test
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d memory <"$repo_root/$migration" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d memory <"$repo_root/$test_sql" >/dev/null
[[ "$(psql_scalar "SELECT encode(public.digest(convert_to(pg_get_functiondef(
  'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
),'UTF8'),'sha256'),'hex')")" == "$expected_function_sha" ]]

phase=postflight
capture_memory_state "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
restore_timers
curl --fail --silent --show-error --max-time 10 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null

phase=report
report="$snapshot_dir/memory_v1_v5_2_employment_v6_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg manifest_sha "$expected_manifest_sha" \
  --arg backup "$backup" \
  --arg qdrant_sha "$qdrant_before" \
  '{contract_version:"memory_v1_v5_2_employment_compiler_v6_install_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    manifest_sha256:$manifest_sha,backup:$backup,qdrant_sha256:$qdrant_sha,
    checks:{fresh_backup:true,hash_locked_migration:true,
      rollback_only_security_test:true,all_memory_rows_unchanged:true,
      qdrant_unchanged:true,account_isolation_preserved:true,
      model_calls:0,claims_created:0,retrieval_changes:0,
      prompt_influence_changes:0,timers_restored:true,service_health:true},
    hard_stop:"before private V5.2 employment canary or scheduler profile change"}' \
  >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_2_employment_compiler_v6_production_apply: PASS\n'
printf 'report=%s\n' "$report"
