#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs three additive, owner-scoped project projection
# functions, runs live rollback-only functional/isolation tests, and proves no
# memory rows or Qdrant points changed. It does not stage a durable projection.

if [[ "${MEMORY_V1_PROJECT_PROJECTION_API_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_PROJECT_PROJECTION_API_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=6dfab00449f45cb63c051b3cb5cd735795d75d61
migration=ops/sql/20260718_memory_v1_v5_project_projection_api.sql
expected_migration_sha=ff583a50bea8558102055606b72b213f963a03d6aed7e629693b026810141dcc
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_project_projection_api_install.lock
phase=initialization
status_file=
units_quiesced=0
unit_state_before=
timers=(
  memory-v1-consolidation.timer
  memory-v1-deferred-reconciliation-scan.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-governance.timer
  memory-v1-projection.timer
  memory-v1-v5-chat-capture.timer
)

[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
  echo "production install requires a clean Git worktree" >&2
  exit 1
}
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == \
  "$expected_migration_sha" ]] || {
  echo "project projection migration SHA drifted" >&2
  exit 1
}

mkdir -p "$snapshot_dir"
exec 9>"$lock_file"
flock -n 9 || {
  echo "another project projection API install holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_project_projection_api_install_${run_id}.status"
unit_state_before="$snapshot_dir/memory_v1_project_projection_api_units_before_${run_id}.tsv"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

restore_units() {
  if [[ "$units_quiesced" -ne 1 || ! -s "$unit_state_before" ]]; then
    return 0
  fi
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]] || return 1
    if [[ "$active" == "active" ]]; then
      sudo systemctl start "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state_before"
  units_quiesced=0
}

record_exit() {
  code=$?
  if [[ "$units_quiesced" -eq 1 ]]; then
    saved_phase=$phase
    phase=restore_timer_state_after_failure
    restore_units || true
    phase=$saved_phase
  fi
  {
    printf 'run_id=%s\n' "$run_id"
    printf 'phase=%s\n' "$phase"
    printf 'exit_code=%s\n' "$code"
    printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$status_file"
  chmod 0600 "$status_file"
}
trap record_exit EXIT

qdrant_signature() {
  curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id)' \
    | sha256sum | awk '{print $1}'
}

capture_memory_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || return 1
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regprocedure('memory.preflight_project_projection_source_v5(uuid)') IS NULL
  AND to_regprocedure('memory.preflight_project_projection_packet_v5(uuid,text)') IS NULL
  AND to_regprocedure('memory.stage_project_projection_plan_v5(uuid,text,text)') IS NULL
)::integer")" == "1" ]] || {
  echo "project projection API already exists" >&2
  exit 1
}

phase=capture_timer_state
: >"$unit_state_before"
for unit in "${timers[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state_before"
done
chmod 0600 "$unit_state_before"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled active; do
  if [[ "$active" == "active" ]]; then
    sudo systemctl stop "$unit"
  fi
done <"$unit_state_before"
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  systemctl is-active --quiet "$service" && {
    echo "$service did not quiesce within 30 seconds" >&2
    exit 1
  }
  [[ "$(systemctl is-active "$unit")" == "inactive" ]]
done <"$unit_state_before"

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_project_projection_api_before_${run_id}.tsv"
post="$snapshot_dir/memory_v1_project_projection_api_after_${run_id}.tsv"
capture_memory_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=backup
backup_partial="$snapshot_dir/.memory_pre_project_projection_api_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_project_projection_api_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=schema_install
install_log="$snapshot_dir/memory_v1_project_projection_api_install_${run_id}.log"
docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=180s' \
  -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
  <"$repo_root/$migration" >"$install_log" 2>&1
chmod 0600 "$install_log"

phase=rolled_back_functional_security_test
set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]] || {
  echo "POSTGRES_DSN is required for rollback-only function tests" >&2
  exit 1
}
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python - <<'PY' \
  >>"$install_log" 2>&1
import asyncio
import json
import os
import uuid

import asyncpg

from scripts.memory_v1_projection_v5_contract_test import (
    owner_manifest_sha256,
    stable_json,
    validate_packet,
)
from scripts.memory_v1_v5_project_projection_preflight import (
    build_packet,
    build_projection,
    load_contract,
    validate_source,
)

OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER = "557ea042-cb82-48f8-9429-472e96c957ef"
OBSERVATION = uuid.UUID("258d8d96-2cbd-4296-b878-769c90533fae")
PLAN = uuid.UUID("858d8d96-2cbd-4296-b878-769c90533fae")


async def main() -> None:
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"], command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("POSTGRES_DSN must use brains_app")
        tx = conn.transaction()
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
        source_row = await conn.fetchrow(
            "SELECT * FROM memory.preflight_project_projection_source_v5($1)",
            OBSERVATION,
        )
        source = dict(source_row)
        if isinstance(source["object_literal"], str):
            source["object_literal"] = json.loads(source["object_literal"])
        validate_source(source)
        projection = build_projection(OWNER, source, "architecture.memory_service")
        packet = build_packet(projection)
        validate_packet(packet, OWNER, load_contract())
        packet_text = stable_json(packet)
        manifest = owner_manifest_sha256(OWNER, packet["packet_sha256"])
        preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_project_projection_packet_v5($1,$2)",
            PLAN,
            packet_text,
        )
        assert preflight["owner_manifest_sha256"] == manifest
        assert preflight["existing_aggregates"] == 0
        assert preflight["existing_plans"] == 0
        applied = await conn.fetchrow(
            "SELECT * FROM memory.stage_project_projection_plan_v5($1,$2,$3)",
            PLAN,
            packet_text,
            manifest,
        )
        assert applied["outcome"] == "applied" and applied["rows_written"] == 4
        replay = await conn.fetchrow(
            "SELECT * FROM memory.stage_project_projection_plan_v5($1,$2,$3)",
            PLAN,
            packet_text,
            manifest,
        )
        assert replay["outcome"] == "replayed" and replay["rows_written"] == 0
        await tx.rollback()

        tx = conn.transaction(readonly=True)
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", OTHER)
        try:
            await conn.fetchrow(
                "SELECT * FROM memory.preflight_project_projection_source_v5($1)",
                OBSERVATION,
            )
        except asyncpg.PostgresError:
            pass
        else:
            raise RuntimeError("cross-owner project source unexpectedly resolved")
        await tx.rollback()
    finally:
        await conn.close()


asyncio.run(main())
print("rollback-only project projection functional/isolation test: PASS")
PY

phase=postflight
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM pg_proc WHERE oid IN (
    'memory.preflight_project_projection_source_v5(uuid)'::regprocedure,
    'memory.preflight_project_projection_packet_v5(uuid,text)'::regprocedure,
    'memory.stage_project_projection_plan_v5(uuid,text,text)'::regprocedure
  ) AND prosecdef AND proowner='memory_v5_writer'::regrole
    AND proconfig @> ARRAY['search_path=\"\"']::text[])=3
  AND has_function_privilege('brains_app','memory.preflight_project_projection_source_v5(uuid)','EXECUTE')
  AND has_function_privilege('brains_app','memory.preflight_project_projection_packet_v5(uuid,text)','EXECUTE')
  AND has_function_privilege('brains_app','memory.stage_project_projection_plan_v5(uuid,text,text)','EXECUTE')
  AND NOT has_function_privilege('public','memory.preflight_project_projection_source_v5(uuid)','EXECUTE')
  AND NOT has_function_privilege('public','memory.preflight_project_projection_packet_v5(uuid,text)','EXECUTE')
  AND NOT has_function_privilege('public','memory.stage_project_projection_plan_v5(uuid,text,text)','EXECUTE')
)::integer")" == "1" ]] || {
  echo "project projection API privilege postflight failed" >&2
  exit 1
}
capture_memory_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo "memory rows changed during project projection API installation" >&2
  exit 1
}
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo "Qdrant changed during project projection API installation" >&2
  exit 1
}

phase=restore_timer_state
restore_units
unit_state_after="$snapshot_dir/memory_v1_project_projection_api_units_after_${run_id}.tsv"
: >"$unit_state_after"
for unit in "${timers[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state_after"
done
chmod 0600 "$unit_state_after"
cmp -s "$unit_state_before" "$unit_state_after" || {
  diff -u "$unit_state_before" "$unit_state_after" >&2 || true
  echo "Memory V1 timer state was not restored exactly" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_project_projection_api_install_${run_id}.json"
REPORT="$report" BACKUP="$backup" CATALOG="$catalog" LOG="$install_log" \
BASELINE="$baseline" POST="$post" UNIT_BEFORE="$unit_state_before" \
UNIT_AFTER="$unit_state_after" QDRANT_BEFORE="$qdrant_before" \
QDRANT_AFTER="$qdrant_after" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
MIGRATION_SHA="$expected_migration_sha" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

report = {
    "contract_version": "memory_v1_v5_project_projection_api_install_report_v1",
    "instruction_source": "user_continue_20260717",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "migration_sha256": os.environ["MIGRATION_SHA"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "evidence": {
        "install_log": os.environ["LOG"],
        "memory_state_before": os.environ["BASELINE"],
        "memory_state_after": os.environ["POST"],
        "timer_state_before": os.environ["UNIT_BEFORE"],
        "timer_state_after": os.environ["UNIT_AFTER"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "hash_locked_migration_installed": True,
        "rollback_only_stage_and_replay_passed": True,
        "cross_owner_rejected": True,
        "least_privilege_verified": True,
        "memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "timer_state_restored": True,
        "durable_projection_staged": False,
        "retrieval_activated": False,
        "prompt_influence": False,
    },
    "hard_stop": "before_durable_project_projection_staging",
}
Path(os.environ["REPORT"]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_project_projection_api_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
