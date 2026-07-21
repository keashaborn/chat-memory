#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the additive, owner-scoped V5.1 relationship
# claim projection contract. It writes no plans, claims, vectors, retrieval
# state, or prompt influence.

if [[ "${MEMORY_V1_RELATIONSHIP_CLAIM_V5_1_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_RELATIONSHIP_CLAIM_V5_1_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=c3b0d86f3db5cb194ddcd5802f59279b596693ca
migration=ops/sql/20260721_memory_v1_relationship_claim_projection_v5_1.sql
rollback=ops/sql/20260721_memory_v1_relationship_claim_projection_v5_1_rollback.sql
expected_migration_sha=f00bf9e65cbc6061d0420cefaab6e9d490e3a71e12444547861300d836b1ee5e
expected_rollback_sha=1a762356c7511eb66b4fdd07adf38af4d19a9224f6d877cbc53f1ca64c0070d2
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
parent_observation=2d797843-b0c4-43fb-a03c-fc4beb658058
health_observation=5954f2cb-43ba-4295-a84a-9fb8e967b695
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_relationship_claim_v5_1_install.lock
phase=initialization
status_file=
units_quiesced=0
migration_installed=0
installation_committed=0
unit_state_before=

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == \
  "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == \
  "$expected_rollback_sha" ]]

mkdir -p "$snapshot_dir"
exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_relationship_claim_v5_1_install_${run_id}.status"
unit_state_before="$snapshot_dir/memory_v1_relationship_claim_v5_1_units_before_${run_id}.tsv"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

run_admin_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database" "$@"
}

restore_units() {
  if [[ "$units_quiesced" -ne 1 || ! -s "$unit_state_before" ]]; then
    return 0
  fi
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state_before"
  units_quiesced=0
}

record_exit() {
  code=$?
  if [[ "$migration_installed" -eq 1 && "$installation_committed" -eq 0 ]]; then
    run_admin_sql <"$repo_root/$rollback" >/dev/null 2>&1 || code=1
  fi
  if [[ "$units_quiesced" -eq 1 ]]; then
    restore_units || code=1
  fi
  {
    printf 'run_id=%s\n' "$run_id"
    printf 'phase=%s\n' "$phase"
    printf 'exit_code=%s\n' "$code"
    printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$status_file"
  chmod 0600 "$status_file"
  exit "$code"
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
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(convert_to(coalesce(string_agg(
               row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
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

service_health() {
  set -a
  source "$repo_root/.env"
  set +a
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

phase=preflight
service_health
[[ "$(psql_scalar "SELECT (
  to_regprocedure('memory.render_relationship_claim_text_v5_1(text,text,text,text,text)') IS NULL
  AND to_regprocedure('memory.preflight_relationship_claim_source_v5_1(uuid)') IS NULL
  AND to_regprocedure('memory.preflight_relationship_claim_packet_v5_1(uuid,text)') IS NULL
  AND to_regprocedure('memory.stage_relationship_claim_plan_v5_1(uuid,text,text)') IS NULL
)::integer")" == 1 ]]

phase=capture_timer_state
: >"$unit_state_before"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state_before"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$unit_state_before" ]]
chmod 0600 "$unit_state_before"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled active; do
  if [[ "$active" == active ]]; then
    sudo -n systemctl stop "$unit"
  fi
done <"$unit_state_before"
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done <"$unit_state_before"

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_relationship_claim_v5_1_before_${run_id}.tsv"
post="$snapshot_dir/memory_v1_relationship_claim_v5_1_after_${run_id}.tsv"
capture_memory_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=backup
backup_partial="$snapshot_dir/.memory_pre_relationship_claim_v5_1_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_relationship_claim_v5_1_${run_id}.dump"
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
install_log="$snapshot_dir/memory_v1_relationship_claim_v5_1_install_${run_id}.log"
docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=180s' \
  -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
  <"$repo_root/$migration" >"$install_log" 2>&1
migration_installed=1
chmod 0600 "$install_log"

phase=owner_and_isolation_probes
set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
owner_probe="$snapshot_dir/memory_v1_relationship_claim_v5_1_owner_probe_${run_id}.tsv"
cross_owner_error="$snapshot_dir/memory_v1_relationship_claim_v5_1_cross_owner_${run_id}.log"
health_error="$snapshot_dir/memory_v1_relationship_claim_v5_1_health_exclusion_${run_id}.log"
psql "$POSTGRES_DSN" -X -A -t -v ON_ERROR_STOP=1 \
  -v owner="$owner" -v observation="$parent_observation" >"$owner_probe" <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id', :'owner', true);
SELECT observation_id::text || E'\t' || predicate || E'\t' || canonical_text
FROM memory.preflight_relationship_claim_source_v5_1(:'observation'::uuid);
ROLLBACK;
SQL
[[ "$(tr -d '\r' <"$owner_probe" | rg -c "relationship.parent_of.*user's parent")" -eq 1 ]]
if psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v owner="$other" -v observation="$parent_observation" \
  >"$cross_owner_error" 2>&1 <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id', :'owner', true);
SELECT * FROM memory.preflight_relationship_claim_source_v5_1(:'observation'::uuid);
ROLLBACK;
SQL
then
  echo 'cross-owner relationship source was visible' >&2
  exit 1
fi
rg -q 'complete accepted owner-scoped V5.1 relationship source not found' \
  "$cross_owner_error"
if psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v owner="$owner" -v observation="$health_observation" \
  >"$health_error" 2>&1 <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id', :'owner', true);
SELECT * FROM memory.preflight_relationship_claim_source_v5_1(:'observation'::uuid);
ROLLBACK;
SQL
then
  echo 'health observation entered relationship projection' >&2
  exit 1
fi
rg -q 'complete accepted owner-scoped V5.1 relationship source not found' \
  "$health_error"
chmod 0600 "$owner_probe" "$cross_owner_error" "$health_error"

phase=acl_and_contract_verification
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM pg_proc WHERE oid IN (
    'memory.preflight_relationship_claim_source_v5_1(uuid)'::regprocedure,
    'memory.preflight_relationship_claim_packet_v5_1(uuid,text)'::regprocedure,
    'memory.stage_relationship_claim_plan_v5_1(uuid,text,text)'::regprocedure
  ) AND prosecdef AND proowner='memory_v5_writer'::regrole
    AND proconfig @> ARRAY['search_path=\"\"']::text[])=3
  AND has_function_privilege('brains_app',
    'memory.preflight_relationship_claim_source_v5_1(uuid)','EXECUTE')
  AND has_function_privilege('brains_app',
    'memory.preflight_relationship_claim_packet_v5_1(uuid,text)','EXECUTE')
  AND has_function_privilege('brains_app',
    'memory.stage_relationship_claim_plan_v5_1(uuid,text,text)','EXECUTE')
  AND NOT has_function_privilege('public',
    'memory.stage_relationship_claim_plan_v5_1(uuid,text,text)','EXECUTE')
  AND pg_get_constraintdef((SELECT oid FROM pg_constraint
    WHERE conrelid='memory.projection_plan'::regclass
      AND conname='projection_plan_predicate_registry_version_check'))
      LIKE '%memory_predicate_registry_v5_1%'
  AND pg_get_constraintdef((SELECT oid FROM pg_constraint
    WHERE conrelid='memory.projection_plan_item'::regclass
      AND conname='projection_plan_item_predicate_registry_version_check'))
      LIKE '%memory_predicate_registry_v5_1%'
)::integer")" == 1 ]]

phase=postflight
capture_memory_state "$post"
cmp -s "$baseline" "$post"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]
service_health

phase=restore_timer_state
restore_units
unit_state_after="$snapshot_dir/memory_v1_relationship_claim_v5_1_units_after_${run_id}.tsv"
: >"$unit_state_after"
while IFS=$'\t' read -r unit _enabled _active; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state_after"
done <"$unit_state_before"
chmod 0600 "$unit_state_after"
cmp -s "$unit_state_before" "$unit_state_after"
service_health

phase=report
report="$snapshot_dir/memory_v1_relationship_claim_v5_1_install_${run_id}.json"
REPORT="$report" BACKUP="$backup" CATALOG="$catalog" LOG="$install_log" \
BASELINE="$baseline" POST="$post" UNIT_BEFORE="$unit_state_before" \
UNIT_AFTER="$unit_state_after" OWNER_PROBE="$owner_probe" \
CROSS_OWNER_ERROR="$cross_owner_error" HEALTH_ERROR="$health_error" \
QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
MIGRATION_SHA="$expected_migration_sha" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

report = {
    "contract_version": "memory_v1_relationship_claim_v5_1_install_report_v1",
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
        "owner_probe": os.environ["OWNER_PROBE"],
        "cross_owner_rejection": os.environ["CROSS_OWNER_ERROR"],
        "health_exclusion": os.environ["HEALTH_ERROR"],
    },
    "verification": {
        "additive_contract_installed": True,
        "owner_parent_source_admitted": True,
        "cross_owner_source_rejected": True,
        "health_source_rejected": True,
        "memory_rows_unchanged": True,
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
        "timers_restored_exactly": True,
        "plans_written": 0,
        "claims_written": 0,
        "qdrant_writes": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    },
    "hard_stop": "before_relationship_plan_staging_review_apply_qdrant_or_prompt_influence",
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
installation_committed=1
phase=complete
printf 'backup=%s\nreport=%s\n' "$backup" "$report"
printf 'memory_v1_relationship_claim_projection_v5_1_production_install: PASS\n'
