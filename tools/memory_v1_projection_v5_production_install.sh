#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. This runner installs schema/functions and executes four
# rollback-only security suites. It never starts extraction, retrieval, workers,
# timers, or durable apply.

if [[ $# -ne 2 ]]; then
  echo "usage: $0 PLAN.json AUTHORIZATION.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_PRODUCTION_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_PRODUCTION_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
plan=$(realpath "$1")
authorization=$(realpath "$2")
verifier="$repo_root/scripts/memory_v1_projection_v5_install_plan.py"
container=brains-postgres-1
database=memory
database_role=sage
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_projection_v5_install.lock
phase=initialization
status_file=

if [[ "$authorization" == "$repo_root"/* ]]; then
  echo "authorization file must be outside the Git worktree" >&2
  exit 1
fi
if [[ -n "$(git -C "$repo_root" status --porcelain)" ]]; then
  echo "production install requires a clean Git worktree" >&2
  exit 1
fi

# Validate the untrusted authorization document before using any of its fields
# in host paths or lock/report identifiers.
python3 "$verifier" \
  --manifest "$plan" \
  --repo-root "$repo_root" \
  --authorization "$authorization" >/dev/null

exec 9>"$lock_file"
flock -n 9 || {
  echo "another Memory V1 V5 installation holds $lock_file" >&2
  exit 1
}
umask 077

auth_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' "$authorization")
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_projection_v5_install_${run_id}.status"

record_exit() {
  exit_code=$?
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_id=%s\n' "$run_id"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
}
trap record_exit EXIT

phase=authorization_verification
preflight="$snapshot_dir/memory_v1_projection_v5_preflight_${run_id}.json"
python3 "$verifier" \
  --manifest "$plan" \
  --repo-root "$repo_root" \
  --authorization "$authorization" \
  --production-preflight \
  --output "$preflight" >/dev/null
chmod 0600 "$preflight"

mapfile -t migrations < <(
  python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" --list-kind migration
)
mapfile -t tests < <(
  python3 "$verifier" --manifest "$plan" --repo-root "$repo_root" --list-kind rolled_back_test
)
[[ ${#migrations[@]} -eq 5 ]] || { echo "expected exactly five migrations" >&2; exit 1; }
[[ ${#tests[@]} -eq 4 ]] || { echo "expected exactly four rollback tests" >&2; exit 1; }

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U "$database_role" -d "$database" -c "$1"
}

run_sql_file() {
  local file=$1
  docker exec \
    -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=120s' \
    -i "$container" \
    psql -X -v ON_ERROR_STOP=1 -U "$database_role" -d "$database" \
    <"$repo_root/$file"
}

phase=baseline_capture
baseline_tables="$snapshot_dir/memory_v1_projection_v5_baseline_tables_${run_id}.txt"
baseline_counts="$snapshot_dir/memory_v1_projection_v5_baseline_counts_${run_id}.tsv"
post_counts="$snapshot_dir/memory_v1_projection_v5_post_counts_${run_id}.tsv"
psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' AND table_name <> 'predicate' ORDER BY table_name" \
  >"$baseline_tables"
: >"$baseline_counts"
while IFS= read -r table; do
  [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || { echo "unsafe table name: $table" >&2; exit 1; }
  printf '%s\t%s\n' "$table" "$(psql_scalar "SELECT count(*) FROM memory.\"$table\"")" \
    >>"$baseline_counts"
done <"$baseline_tables"
chmod 0600 "$baseline_tables" "$baseline_counts"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_projection_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_projection_${run_id}.dump"
catalog="$backup.catalog"
checksum="$backup.sha256"
docker exec "$container" pg_dump -U "$database_role" -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]] || { echo "backup is empty" >&2; exit 1; }
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]] || { echo "backup restore catalog is empty" >&2; exit 1; }
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$checksum"
chmod 0600 "$checksum"

# A slow backup must not let an expired authorization cross the first schema
# write boundary.
python3 "$verifier" \
  --manifest "$plan" \
  --repo-root "$repo_root" \
  --authorization "$authorization" >/dev/null

phase=schema_install
install_log="$snapshot_dir/memory_v1_projection_v5_install_${run_id}.log"
: >"$install_log"
for migration in "${migrations[@]}"; do
  printf 'INSTALL %s\n' "$migration" >>"$install_log"
  run_sql_file "$migration" >>"$install_log" 2>&1
done
chmod 0600 "$install_log"

phase=rolled_back_security_tests
for test_file in "${tests[@]}"; do
  printf 'TEST %s\n' "$test_file" >>"$install_log"
  run_sql_file "$test_file" >>"$install_log" 2>&1
done

phase=postflight
: >"$post_counts"
while IFS= read -r table; do
  printf '%s\t%s\n' "$table" "$(psql_scalar "SELECT count(*) FROM memory.\"$table\"")" \
    >>"$post_counts"
done <"$baseline_tables"
chmod 0600 "$post_counts"
cmp -s "$baseline_counts" "$post_counts" || {
  diff -u "$baseline_counts" "$post_counts" >&2 || true
  echo "preexisting durable table counts changed" >&2
  exit 1
}

mapfile -t empty_tables < <(
  python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["postinstall_requirements"]["empty_tables"]))' "$plan"
)
for qualified in "${empty_tables[@]}"; do
  [[ "$qualified" =~ ^memory\.[a-z][a-z0-9_]*$ ]] || { echo "unsafe empty-table name" >&2; exit 1; }
  table=${qualified#memory.}
  [[ "$(psql_scalar "SELECT count(*) FROM memory.\"$table\"")" == "0" ]] || {
    echo "$qualified is not empty after rollback-only tests" >&2
    exit 1
  }
  for privilege in SELECT INSERT UPDATE DELETE TRUNCATE REFERENCES TRIGGER; do
    [[ "$(psql_scalar "SELECT has_table_privilege('brains_app', '$qualified', '$privilege')")" == "f" ]] || {
      echo "brains_app unexpectedly has $privilege on $qualified" >&2
      exit 1
    }
  done
done

role_safe=$(psql_scalar "SELECT (NOT rolcanlogin AND NOT rolinherit AND NOT rolbypassrls AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole)::text FROM pg_roles WHERE rolname='memory_v5_writer'")
[[ "$role_safe" == "true" ]] || { echo "memory_v5_writer role attributes are unsafe" >&2; exit 1; }

registry_state=$(psql_scalar "SELECT concat_ws('|', registry_version, registry_sha256, status, runtime_active::text, (SELECT count(*) FROM memory.predicate_contract WHERE registry_version='memory_predicate_registry_v5')) FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5'")
expected_registry='memory_predicate_registry_v5|4d626433109c89c18d5ea374e173ca6785de6f9c20ecc05fef9f6447bfc671f4|proposed|false|44'
[[ "$registry_state" == "$expected_registry" ]] || { echo "predicate registry state mismatch" >&2; exit 1; }

phase=report
report="$snapshot_dir/memory_v1_projection_v5_install_${run_id}.json"
PLAN="$plan" AUTHORIZATION="$authorization" PREFLIGHT="$preflight" BACKUP="$backup" \
  BACKUP_SHA="$checksum" CATALOG="$catalog" INSTALL_LOG="$install_log" \
  BASELINE_COUNTS="$baseline_counts" POST_COUNTS="$post_counts" REPORT="$report" \
  python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

plan = json.loads(Path(os.environ["PLAN"]).read_text())
authorization = json.loads(Path(os.environ["AUTHORIZATION"]).read_text())
preflight = json.loads(Path(os.environ["PREFLIGHT"]).read_text())
report = {
    "contract_version": "memory_v1_projection_v5_production_install_report_v1",
    "authorization_id": authorization["authorization_id"],
    "plan_id": plan["plan_id"],
    "plan_sha256": authorization["plan_sha256"],
    "expected_head_commit": authorization["expected_head_commit"],
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "target": plan["target"],
    "backup": {
        "path": os.environ["BACKUP"],
        "sha256_file": os.environ["BACKUP_SHA"],
        "restore_catalog": os.environ["CATALOG"],
    },
    "evidence": {
        "preflight": os.environ["PREFLIGHT"],
        "install_log": os.environ["INSTALL_LOG"],
        "baseline_counts": os.environ["BASELINE_COUNTS"],
        "post_counts": os.environ["POST_COUNTS"],
    },
    "checks": {
        "preflight_ready": preflight["production_preflight"]["ready_for_authorization"],
        "five_schema_migrations_installed": True,
        "four_security_suites_rolled_back": True,
        "preexisting_user_table_counts_unchanged": True,
        "all_v5_owner_and_staging_tables_empty": True,
        "memory_v5_writer_restricted": True,
        "brains_app_has_no_direct_v5_table_access": True,
        "predicate_registry_proposed_and_runtime_inactive": True,
        "live_extraction_or_apply_invoked": False,
        "runtime_activated": False,
    },
    "hard_stop": "before_live_extraction_or_durable_application",
}
Path(os.environ["REPORT"]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_projection_v5_production_install: PASS\n'
printf 'report=%s\n' "$report"
printf 'backup=%s\n' "$backup"
