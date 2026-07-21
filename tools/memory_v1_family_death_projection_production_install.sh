#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the reviewed parent-role and death-event
# projection compatibility layers. It writes no memory data or Qdrant points.

if [[ "${MEMORY_V1_FAMILY_DEATH_PROJECTION_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_FAMILY_DEATH_PROJECTION_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
isolated_ancestor=9ecb2b6
production_ancestor=4928cf8
role_migration=ops/sql/20260721_memory_v1_role_only_family_resolution_v5_1.sql
event_migration=ops/sql/20260721_memory_v1_life_event_claim_projection_v5_1.sql
security_test=tests/memory_v1_family_death_projection_install.sql
role_sha=8a0b901011eae75b6d38ef21e26afce2c6c77a022713341f705ef8d3ffa5c558
event_sha=70165531c68c74968e301b3a6fffbee654417c8ed871eac3a03b15ec405b9b51
test_sha=1fa6be10ea318563cf01a5798c0624dead2205b27564bdeea34464601e6064c9
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_family_death_projection_install.lock
phase=initialization
units_quiesced=0
status_file=
unit_state=$(mktemp /tmp/memory-v1-family-death-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-family-death-tables.XXXXXX)

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

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
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(convert_to(coalesce(string_agg(
               row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

restore_timers() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

record_exit() {
  code=$?
  if [[ "$units_quiesced" -eq 1 ]]; then restore_timers || code=1; fi
  rm -f "$unit_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
}
trap record_exit EXIT

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
if ! git -C "$repo_root" merge-base --is-ancestor "$isolated_ancestor" HEAD \
   && ! git -C "$repo_root" merge-base --is-ancestor "$production_ancestor" HEAD; then
  echo 'required family/death projection implementation is absent' >&2
  exit 1
fi
[[ "$(sha256sum "$repo_root/$role_migration" | awk '{print $1}')" == "$role_sha" ]]
[[ "$(sha256sum "$repo_root/$event_migration" | awk '{print $1}')" == "$event_sha" ]]
[[ "$(sha256sum "$repo_root/$security_test" | awk '{print $1}')" == "$test_sha" ]]

set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${VS_SERVICE_TOKEN:-}" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_family_death_projection_install_${run_id}.status"
before="$snapshot_dir/memory_v1_family_death_projection_before_${run_id}.tsv"
after="$snapshot_dir/memory_v1_family_death_projection_after_${run_id}.tsv"
log="$snapshot_dir/memory_v1_family_death_projection_install_${run_id}.log"

phase=capture_timer_state
: >"$unit_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$unit_state" ]]
chmod 0600 "$unit_state"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled active; do
  [[ "$active" != active ]] || sudo -n systemctl stop "$unit"
done <"$unit_state"
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"

phase=capture_baseline
psql_scalar "SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)

phase=backup
backup_partial="$snapshot_dir/.memory_pre_family_death_projection_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_family_death_projection_${run_id}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=install_schema
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$role_migration" >"$log" 2>&1
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$event_migration" >>"$log" 2>&1
chmod 0600 "$log"

phase=rollback_only_security_test
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  <"$repo_root/$security_test" >>"$log" 2>&1

phase=postflight
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM memory.entity_role_resolution_v5_1)=0
  AND pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid=
    'memory.entity_role_resolution_v5_1'::regclass))='memory_v5_writer'
  AND NOT has_table_privilege('brains_app',
    'memory.entity_role_resolution_v5_1','INSERT')
  AND has_function_privilege('brains_app',
    'memory.preflight_role_only_family_resolution_v5_1(uuid,uuid,text)','EXECUTE')
  AND has_function_privilege('brains_app',
    'memory.preflight_claim_projection_source_v5_1(uuid)','EXECUTE')
  AND to_regprocedure(
    'memory.preflight_claim_projection_source_v5_1_base(uuid)') IS NOT NULL
)::int")" == 1 ]]
capture_state "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=restore_timers
restore_timers
curl --fail --silent --max-time 5 -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
curl --fail --silent --max-time 5 -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/readyz | jq -e '.ok==true and .postgres==true' >/dev/null

phase=report
report="$snapshot_dir/memory_v1_family_death_projection_install_${run_id}.json"
REPORT="$report" BACKUP="$backup" LOG="$log" BEFORE="$before" AFTER="$after" \
QDRANT="$qdrant_before" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

report = {
    "contract_version": "memory_v1_family_death_projection_install_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "backup": os.environ["BACKUP"],
    "evidence": {
        "install_log": os.environ["LOG"],
        "memory_before": os.environ["BEFORE"],
        "memory_after": os.environ["AFTER"],
    },
    "verification": {
        "schema_functions_only": True,
        "rollback_only_security_test": "pass",
        "memory_rows_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "timers_restored_exactly": True,
        "claims_written": 0,
        "prompt_influence": False,
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

phase=complete
printf 'backup=%s\nreport=%s\n' "$backup" "$report"
printf 'memory_v1_family_death_projection_production_install: PASS\n'
