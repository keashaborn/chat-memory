#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the reviewed terminal route for deferral-only
# packets that request review but contain no stageable relational content.
# Writes exactly one owner-scoped append-only disposition and nothing else.

if [[ "${MEMORY_V1_V5_MANUAL_DEFERRAL_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_MANUAL_DEFERRAL_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=5c4594d6-314d-5629-8f71-ba469074ab25
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
router_service=memory-v1-v5-local-packet-router.service
migration=ops/sql/20260719_memory_v1_v5_manual_deferral_terminal.sql
rollback=ops/sql/20260719_memory_v1_v5_manual_deferral_terminal_rollback.sql
sql_test=tests/memory_v1_v5_manual_deferral_terminal.sql
disposition_worker=scripts/memory_v1_v5_local_packet_disposition.py
disposition_test=scripts/memory_v1_v5_local_packet_disposition_test.py
router=scripts/memory_v1_v5_local_packet_router.py
clone_test=tools/memory_v1_v5_manual_deferral_terminal_clone.sh
expected_base=5a4f8979ffc9b19670ff542c4fb09a43ffc9c992
lock_file=/home/ubuntu/brains/.memory_v1_v5_manual_deferral_install.lock

declare -A expected_sha256=(
  ["$migration"]="bbf576e89c1b595204103c1b13908591f8c1809d4db391eca7c505602e9073a9"
  ["$rollback"]="e2860dda34b03c2ed38421549e8e61219ed897f05d684f6e8b0559434dbf77bb"
  ["$sql_test"]="e6c67837b178870b4ae60692bceb58c6453fa9eeee5bf26a96d8cb06a6d6e424"
  ["$disposition_worker"]="c98459ff7d4ad6e0e1a10050e7037672ed479f9928872803d52c8b2738e96c65"
  ["$disposition_test"]="66a4a640582df11b7f24b07c80db4f4716a2222d453e998447062e7d68ee83a6"
  ["$router"]="08f7434514805a17897447b89235efa6e3495a59795016ce66ce5c5855afa9a5"
  ["$clone_test"]="b26be47bd648dff683763eb9526037d736dc8b8a1ccbbcbd2ba95c920689c6d0"
)

phase=initialization
run_tag=
status_file=
timers_quiesced=0
schema_installed=0
data_written=0
installation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-manual-deferral-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-manual-deferral-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-manual-deferral-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-manual-deferral-after.XXXXXX.tsv)
restored=$(mktemp /tmp/memory-v1-v5-manual-deferral-restored.XXXXXX.tsv)
plan=$(mktemp /tmp/memory-v1-v5-manual-deferral-plan.XXXXXX.json)
other_plan=$(mktemp /tmp/memory-v1-v5-manual-deferral-other.XXXXXX.json)
applied=$(mktemp /tmp/memory-v1-v5-manual-deferral-applied.XXXXXX.json)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" "$restored" \
  "$plan" "$other_plan" "$applied"

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
  rm -f "$timer_state" "$table_list" "$before" "$after" "$restored" \
    "$plan" "$other_plan" "$applied"
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

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

review_root_signature() {
  (
    cd "$review_root"
    find . -type f -print0 | sort -z | xargs -0r sha256sum
  ) | sha256sum | awk '{print $1}'
}

disposition_other_signature() {
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

for required in "${!expected_sha256[@]}"; do
  [[ -f "$required" ]]
  [[ "$(sha256sum "$required" | awk '{print $1}')" \
      == "${expected_sha256[$required]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$expected_base" HEAD
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
[[ -d "$review_root" ]]
[[ "$(stat -c '%a:%U:%G' "$review_root")" == 700:ubuntu:ubuntu ]]
bash -n "$clone_test"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$disposition_test" \
  >/dev/null

phase=clone_verification
"$clone_test" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_manual_deferral_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_manual_deferral_${run_tag}.json"

phase=quiesce_timers
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$timer_state")" -ge 1 ]]
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
backup_partial="$snapshot_dir/.memory_pre_v5_manual_deferral_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_manual_deferral_${run_tag}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner >"$backup_partial"
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
  WHERE table_type='BASE TABLE'
    AND table_schema IN ('memory','public')
    AND NOT (
      table_schema='memory' AND table_name='v5_local_packet_disposition'
    )
  ORDER BY table_schema,table_name
" >"$table_list"
[[ -s "$table_list" ]]
capture_state "$before"
target_before=$(scalar "SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")
[[ "$target_before" == 0 ]]
other_dispositions_before=$(disposition_other_signature)
qdrant_before=$(qdrant_signature)
review_root_before=$(review_root_signature)

phase=install_schema
run_sql <"$migration" >/dev/null
schema_installed=1
packet_storage_sha256=$(scalar "SELECT packet_storage_sha256
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND manual_review_required AND entity_mention_count=0
    AND observation_count=0 AND comparison_hint_count=0
    AND deferral_count>0")
[[ "$packet_storage_sha256" =~ ^[0-9a-f]{64}$ ]]

phase=rollback_only_security_test
run_sql -v owner_user_id="$owner" -v other_owner_user_id="$other" \
  -v packet_id="$packet" -v packet_storage_sha256="$packet_storage_sha256" \
  <"$sql_test" >/dev/null
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")" == 0 ]]

phase=dry_run
set -a
source .env
set +a
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$router" \
  --owner-user-id "$owner" --review-root "$review_root" >"$plan"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$router" \
  --owner-user-id "$other" --review-root "$review_root" >"$other_plan"
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
MEMORY_V1_V5_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_local_packet_router_apply_v1 \
  /opt/chat-memory/venv/bin/python "$router" \
  --owner-user-id "$owner" --review-root "$review_root" --apply >"$applied"
if [[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND reason_code='deferral_only_review_unresolved'")" == 1 ]]; then
  data_written=1
fi
[[ "$data_written" -eq 1 ]]
jq -e '.apply==true and .outcome=="terminal_no_stage" and
  .write_counts.packet_route_events==1 and
  .write_counts.restricted_review_artifacts==0 and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .zero_write_replay_proved==true and .external_model_calls==0' \
  "$applied" >/dev/null

phase=postflight
[[ "$(disposition_other_signature)" == "$other_dispositions_before" ]]
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(review_root_signature)" == "$review_root_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim")" -ge 0 ]]

phase=restore_timers
sudo -n systemctl reset-failed "$router_service" || true
restore_timers
capture_state "$restored"
cmp -s "$before" "$restored"
qdrant_restored=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_restored" ]]
[[ "$(systemctl is-failed "$router_service" || true)" != failed ]]
installation_committed=1

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg owner_sha256 "$(printf %s "$owner" | sha256sum | awk '{print $1}')" \
  --arg packet_sha256 "$(printf %s "$packet" | sha256sum | awk '{print $1}')" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_restored" \
  '{contract_version:"memory_v1_v5_manual_deferral_terminal_install_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    owner_user_id_sha256:$owner_sha256,packet_id_sha256:$packet_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    result:{terminal_dispositions_written:1,other_owner_rows_written:0,
      review_artifacts_written:0,stage_rows_written:0,claims_written:0,
      qdrant_writes:0,prompt_influence:0,external_model_calls:0},
    checks:{hash_locked_inputs:true,production_clone_passed:true,
      fresh_backup:true,rollback_only_security_test:true,
      owner_isolation:true,zero_write_replay:true,
      non_target_tables_unchanged:true,review_root_unchanged:true,
      qdrant_unchanged:true,timers_restored_exactly:true,
      failed_service_cleared:true},qdrant_sha256:$qdrant_sha256}' >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|all(.value==true)' "$report" >/dev/null
printf '%s\n' "$report"
