#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the generic reviewed-observation stage bridge as
# dormant code/schema. It does not admit records or enable the recurring timer.

[[ "$EUID" -eq 0 ]]
[[ "${MEMORY_V1_V5_2_REVIEWED_STAGE_INSTALL:-}" == authorized ]]

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"
[[ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain)" ]]
production_root=/opt/chat-memory
authorized_runtime_head=af471f3e4bcf754044f77ee5929916108426b2e0
production_head=$(git -C "$production_root" rev-parse HEAD)
[[ "$production_head" == "$authorized_runtime_head" ]]
[[ -z "$(GIT_OPTIONAL_LOCKS=0 git -C "$production_root" status --porcelain)" ]]

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ -n "${VS_SERVICE_TOKEN:-}" ]]

container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_reviewed_stage_install.lock
migration=ops/sql/20260731_memory_v1_v5_2_reviewed_stage_successor_v1.sql
rollback=ops/sql/20260731_memory_v1_v5_2_reviewed_stage_successor_v1_rollback.sql
worker=scripts/memory_v1_v5_2_reviewed_observation_stage.py
unit_test=tests/test_memory_v1_v5_2_reviewed_observation_stage.py
clone_test=tools/memory_v1_v5_2_reviewed_stage_successor_clone.sh
service_unit=ops/systemd/memory-v1-v5-2-reviewed-observation-stage.service
timer_unit=ops/systemd/memory-v1-v5-2-reviewed-observation-stage.timer

declare -A expected_sha256=(
  ["$migration"]="8845091e4057e79ee3517bf38e44698918420b3aeeb30c044b3889c43cefc3ce"
  ["$rollback"]="45dd9309f9a60ab2e29bcd66f76dcd8e0942a48a9e836ca522c60f3edc776276"
  ["$worker"]="5fcc6b24643d19a709e1ac90e63ff050db81d57d4e957de68468c057c19688eb"
  ["$unit_test"]="ddcd70c46a30dad1a331296242c602993674921c8b7d5fa41e0e308b0a57a7ef"
  ["$clone_test"]="c795cf4b8f014696cd2b66fe85c1031b5eb651637f29f5313dc1fef6e849252b"
  ["$service_unit"]="2da2e41ef58c52ca0e8271395aae0e947d03a1b7eae649c91d0a9ff3d54f4069"
  ["$timer_unit"]="5514b7b71d8976ea0f8053721c701f3926638dc038ffdc4260e1b26a07ee9027"
)

timer_state=$(mktemp /tmp/memory-reviewed-stage-install-timers.XXXXXX)
table_counts_before=$(mktemp /tmp/memory-reviewed-stage-install-counts-before.XXXXXX)
table_counts_after=$(mktemp /tmp/memory-reviewed-stage-install-counts-after.XXXXXX)
dry_report=$(mktemp /tmp/memory-reviewed-stage-install-dry.XXXXXX)
clone_output=$(mktemp /tmp/memory-reviewed-stage-install-clone.XXXXXX)
chmod 0600 "$timer_state" "$table_counts_before" "$table_counts_after" \
  "$dry_report" "$clone_output"

phase=initialization
timers_quiesced=0
migration_installed=0
completed=0
status_file=

scalar() {
  docker exec "$container" psql -X -q -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
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

capture_table_counts() {
  local output=$1 table count
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    count=$(scalar "SELECT count(*) FROM memory.\"$table\"")
    printf '%s\t%s\n' "$table" "$count" >>"$output"
  done < <(docker exec "$container" psql -X -q -A -t \
    -v ON_ERROR_STOP=1 -U sage -d "$database" -c "
      SELECT table_name
      FROM information_schema.tables
      WHERE table_schema='memory' AND table_type='BASE TABLE'
      ORDER BY table_name")
  chmod 0600 "$output"
}

authenticated_health() {
  [[ "$(systemctl is-active brains.service)" == active ]]
  curl --fail --silent --max-time 5 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --max-time 5 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    case "$enabled" in
      enabled) systemctl enable "$unit" >/dev/null ;;
      disabled) systemctl disable "$unit" >/dev/null ;;
      *) return 1 ;;
    esac
    case "$active" in
      active) systemctl start "$unit" ;;
      inactive) systemctl stop "$unit" ;;
      *) return 1 ;;
    esac
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

on_exit() {
  code=$?
  trap - EXIT
  if [[ "$migration_installed" -eq 1 && "$completed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || code=1
  fi
  restore_timers || code=1
  rm -f "$timer_state" "$table_counts_before" "$table_counts_after" \
    "$dry_report" "$clone_output"
  if [[ -n "$status_file" ]]; then
    {
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$code"
      printf 'completed=%s\n' "$completed"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
}
trap on_exit EXIT

for artifact in "${!expected_sha256[@]}"; do
  [[ -f "$artifact" ]]
  [[ "$(sha256sum "$artifact" | awk '{print $1}')" == \
    "${expected_sha256[$artifact]}" ]]
done
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$unit_test"
bash -n "$clone_test"
systemd-analyze verify "$service_unit" "$timer_unit"
authenticated_health

phase=clone_verification
"$clone_test" >"$clone_output"
clone_artifact_dir=$(awk -F= '/^ARTIFACT_DIR=/{print $2}' "$clone_output" | tail -1)
[[ -n "$clone_artifact_dir" ]]
clone_report="$clone_artifact_dir/report.json"
[[ -f "$clone_report" ]]
jq -e '.clone_passed==true and .selected_records==3 and
  .database_rows_created==6 and .entailment_ready_observations==3 and
  .zero_write_replay==true and .cross_owner_rejected==true and
  .owner_rejected_packets_excluded==true and
  .acl_verified==true and
  .protected_stores_unchanged==true and .production_unchanged==true and
  .qdrant_unchanged==true and .model_calls==0 and .claims==0 and
  .prompt_influence==0' "$clone_report" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_root/memory_v1_v5_2_reviewed_stage_install_${run_tag}.status"
report_file="$snapshot_root/memory_v1_v5_2_reviewed_stage_install_${run_tag}.json"

phase=inventory_timers
while IFS= read -r unit; do
  enabled=$(systemctl is-enabled "$unit" || true)
  active=$(systemctl is-active "$unit" || true)
  [[ "$enabled" == enabled || "$enabled" == disabled ]]
  [[ "$active" == active || "$active" == inactive ]]
  printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$timer_state" ]]

phase=quiesce_timers
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
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
partial="$snapshot_root/.memory_pre_reviewed_stage_install_${run_tag}.dump.partial"
backup="$snapshot_root/memory_pre_reviewed_stage_install_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" -Fc >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
backup_sha256=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha256" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
capture_table_counts "$table_counts_before"
qdrant_before=$(qdrant_signature)
stage_before=$(scalar "
  SELECT concat_ws(':',
    (SELECT count(*) FROM memory.v5_local_packet_stage_admission),
    (SELECT count(*) FROM memory.v5_2_reviewed_observation_stage_admission))")

phase=install_schema
run_sql <"$migration" >/dev/null
migration_installed=1

[[ "$(scalar "
  SELECT proowner::regrole::text
  FROM pg_proc
  WHERE oid='memory.v5_2_resolution_successor_source_v1(uuid)'::regprocedure
")" == memory_v5_writer ]]
[[ "$(scalar "
  SELECT proowner::regrole::text
  FROM pg_proc
  WHERE oid='memory.owner_packet_stage_eligible_v1(uuid)'::regprocedure
")" == sage ]]
[[ "$(scalar "
  SELECT has_function_privilege('brains_app',
    'memory.v5_2_resolution_successor_source_v1(uuid)','EXECUTE')::text
")" == false ]]
[[ "$(scalar "
  SELECT has_function_privilege('brains_app',
    'memory.owner_packet_stage_eligible_v1(uuid)','EXECUTE')::text
")" == false ]]
[[ "$(scalar "
  SELECT has_function_privilege(
    'memory_v5_2_reviewed_observation_stage_maintainer',
    'memory.v5_2_resolution_successor_source_v1(uuid)','EXECUTE')::text
")" == true ]]
[[ "$(scalar "
  SELECT has_function_privilege(
    'memory_v5_2_reviewed_observation_stage_maintainer',
    'memory.owner_packet_stage_eligible_v1(uuid)','EXECUTE')::text
")" == true ]]
[[ "$(scalar "
  SELECT has_function_privilege('brains_app',
    'memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)',
    'EXECUTE')::text
")" == true ]]

phase=live_zero_write_preflight
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --max-records 20 >"$dry_report"
jq -e '.apply==false and .selected_records==3 and
  .database_rows_created==0 and .local_model_calls==0 and
  .external_model_calls==0 and .claims==0 and .qdrant_writes==0 and
  .prompt_influence==0' "$dry_report" >/dev/null

POSTGRES_DSN="$POSTGRES_DSN" OWNER="$owner" OTHER_OWNER="$other_owner" \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import os
import uuid

import asyncpg


async def routes(conn: asyncpg.Connection, owner: uuid.UUID) -> set[uuid.UUID]:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        rows = await conn.fetch(
            "SELECT route_event_id FROM "
            "memory.plan_owner_v5_2_reviewed_observation_stage_v1(20)"
        )
    return {row["route_event_id"] for row in rows}


async def main() -> None:
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"], ssl=False)
    try:
        target = await routes(conn, uuid.UUID(os.environ["OWNER"]))
        other = await routes(conn, uuid.UUID(os.environ["OTHER_OWNER"]))
        if len(target) != 3 or target & other:
            raise RuntimeError("owner isolation or expected target count failed")
    finally:
        await conn.close()


asyncio.run(main())
PY

capture_table_counts "$table_counts_after"
qdrant_after=$(qdrant_signature)
stage_after=$(scalar "
  SELECT concat_ws(':',
    (SELECT count(*) FROM memory.v5_local_packet_stage_admission),
    (SELECT count(*) FROM memory.v5_2_reviewed_observation_stage_admission))")
cmp -s "$table_counts_before" "$table_counts_after"
[[ "$qdrant_after" == "$qdrant_before" ]]
[[ "$stage_after" == "$stage_before" ]]
authenticated_health

phase=restore_timers
restore_timers
authenticated_health

jq -n \
  --arg contract_version memory_v1_v5_2_reviewed_stage_install_report_v1 \
  --arg production_head "$production_head" \
  --arg installer_head "$(git rev-parse HEAD)" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha256" \
  --arg migration_sha256 "${expected_sha256[$migration]}" \
  --arg rollback_sha256 "${expected_sha256[$rollback]}" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:$contract_version,installed:true,
    production_head:$production_head,installer_head:$installer_head,
    backup:$backup,backup_sha256:$backup_sha256,
    migration_sha256:$migration_sha256,rollback_sha256:$rollback_sha256,
    live_selected_records:3,database_rows_created:0,model_calls:0,
    claims:0,qdrant_writes:0,prompt_influence:0,
    owner_isolation:true,table_counts_unchanged:true,qdrant_unchanged:true,
    timer_state_restored:true,worker_enabled:false,
    qdrant_sha256:$qdrant_sha256}' | tee "$report_file"

completed=1
printf 'REPORT=%s\n' "$report_file"
