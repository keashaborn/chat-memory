#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the append-only owner-scoped local-packet
# disposition path and terminally records exactly one verified deferral-only
# packet. Claims, relational staging, Qdrant, prompts, and other owners remain
# unchanged.

if [[ "${MEMORY_V1_V5_LOCAL_DISPOSITION_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LOCAL_DISPOSITION_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=50c8fb51-f847-5423-b9b6-285c79703270
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
migration=ops/sql/20260718_memory_v1_v5_local_packet_disposition.sql
rollback=ops/sql/20260718_memory_v1_v5_local_packet_disposition_rollback.sql
sql_test=tests/memory_v1_v5_local_packet_disposition.sql
worker=scripts/memory_v1_v5_local_packet_disposition.py
worker_test=scripts/memory_v1_v5_local_packet_disposition_test.py
expected_base=1439968
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_disposition_install.lock

declare -A expected_sha256=(
  ["$migration"]="f09e7c2eb6aaa75ea085c352f480bee9daf1258b43bbe3444fd8829c020841c3"
  ["$rollback"]="a973a725264dd3b05e53259d3849207eef45bccf3c36eb7f400be9e9ba8c0ebf"
  ["$sql_test"]="78c3d4b88cc5e1dbfa07080112ce60228a14e2afa3dd5fdf6ff142571615b995"
  ["$worker"]="b63a63a5e352484b3ad010e1a849de2ba77043e249adf3cad5c5e4ba4dd09cb6"
  ["$worker_test"]="da89081ec37891a60d28bb653b4e81ed08c675dad9f36f6ebc97f622061d724d"
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
schema_installed=0
data_written=0
installation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-local-disposition-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-local-disposition-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-local-disposition-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-local-disposition-after.XXXXXX.tsv)
plan=$(mktemp /tmp/memory-v1-v5-local-disposition-plan.XXXXXX.json)
other_plan=$(mktemp /tmp/memory-v1-v5-local-disposition-other.XXXXXX.json)
applied=$(mktemp /tmp/memory-v1-v5-local-disposition-applied.XXXXXX.json)
replayed=$(mktemp /tmp/memory-v1-v5-local-disposition-replayed.XXXXXX.json)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" "$plan" \
  "$other_plan" "$applied" "$replayed"

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database" "$@"
}

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

query_rows() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
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
  rm -f "$timer_state" "$table_list" "$before" "$after" "$plan" \
    "$other_plan" "$applied" "$replayed"
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

capture_existing_tables() {
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
git merge-base --is-ancestor "$expected_base" HEAD
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
[[ "$(scalar "SELECT to_regclass('memory.v5_local_packet_disposition') IS NULL")" == t ]]
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$worker_test" >/dev/null
bash -n tools/memory_v1_v5_local_packet_disposition_production_clone.sh

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_local_disposition_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_local_disposition_${run_tag}.json"

phase=quiesce_timers
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$timer_state")" -eq 7 ]]
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

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_local_disposition_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_local_disposition_${run_tag}.dump"
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
query_rows "
  SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name
" >"$table_list"
[[ -s "$table_list" ]]
capture_existing_tables "$before"
qdrant_before=$(qdrant_signature)

phase=install_schema
run_sql <"$migration" >/dev/null
schema_installed=1
packet_storage_sha256=$(scalar "
  SELECT packet_storage_sha256
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
")
[[ "$packet_storage_sha256" =~ ^[0-9a-f]{64}$ ]]

phase=rollback_only_security_test
run_sql \
  -v owner_user_id="$owner" \
  -v other_owner_user_id="$other" \
  -v packet_id="$packet" \
  -v packet_storage_sha256="$packet_storage_sha256" \
  <"$sql_test" >/dev/null
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == 0 ]]

phase=dry_run
set -a
source .env
set +a
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" >"$plan"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$other" >"$other_plan"
PLAN="$plan" OTHER_PLAN="$other_plan" PACKET="$packet" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

plan=json.loads(Path(os.environ['PLAN']).read_text())
other=json.loads(Path(os.environ['OTHER_PLAN']).read_text())
packet_hash=hashlib.sha256(os.environ['PACKET'].encode()).hexdigest()
assert plan['apply'] is False and plan['database_writes']==0
assert plan['plans'][0]['route']=='terminal_deferral'
assert plan['plans'][0]['packet_id_sha256']==packet_hash
assert all(row.get('packet_id_sha256')!=packet_hash for row in other['plans'])
assert 'source_text' not in json.dumps([plan,other])
PY

phase=apply_one_disposition
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
MEMORY_V1_V5_LOCAL_PACKET_DISPOSITION_APPLY=memory_v1_v5_local_packet_disposition_apply_v1 \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --apply >"$applied"
if [[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == 1 ]]; then
  data_written=1
fi
[[ "$data_written" -eq 1 ]]
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
MEMORY_V1_V5_LOCAL_PACKET_DISPOSITION_APPLY=memory_v1_v5_local_packet_disposition_apply_v1 \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --apply >"$replayed"
APPLIED="$applied" REPLAYED="$replayed" python3 - <<'PY'
import json
import os
from pathlib import Path

applied=json.loads(Path(os.environ['APPLIED']).read_text())
replayed=json.loads(Path(os.environ['REPLAYED']).read_text())
assert applied['outcome']=='terminal_no_stage'
assert applied['write_counts']['dispositions']==1
assert applied['zero_write_replay_proved'] is True
assert replayed['outcome'] in {'no_work','manual_review_pending'}
assert replayed['write_counts']['dispositions']==0
assert replayed['external_model_calls']==0
assert 'source_text' not in json.dumps([applied,replayed])
PY

phase=postflight
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND disposition='terminal_no_stage'
    AND reason_code='deferral_only_no_stage'
")" == 1 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id<>'$owner'::uuid
")" == 0 ]]
capture_existing_tables "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

phase=restore_timers
restore_timers
installation_committed=1

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg owner_sha256 "$(printf %s "$owner" | sha256sum | awk '{print $1}')" \
  --arg packet_sha256 "$(printf %s "$packet" | sha256sum | awk '{print $1}')" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_local_packet_disposition_install_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    owner_user_id_sha256:$owner_sha256,packet_id_sha256:$packet_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    result:{dispositions_written:1,other_owner_rows_written:0,
      stage_rows_written:0,claims_written:0,qdrant_writes:0,
      prompt_influence:0,external_model_calls:0},
    checks:{hash_locked_inputs:true,fresh_backup:true,
      rollback_only_security_test:true,owner_isolation:true,
      other_owner_rows_unchanged:true,zero_write_replay:true,
      existing_tables_unchanged:true,qdrant_unchanged:true,
      timers_restored_exactly:true},qdrant_sha256:$qdrant_sha256}' \
  >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|all(.value==true)' "$report" >/dev/null
printf '%s\n' "$report"
