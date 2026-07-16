#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the owner-self bootstrap schema/functions and
# runs the synthetic owner-isolation suite inside its own rollback transaction.
# It does not create a live owner-self entity or invoke extraction/retrieval.

if [[ $# -ne 1 ]]; then
  echo "usage: $0 AUTHORIZATION.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_OWNER_SELF_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_OWNER_SELF_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
authorization=$(realpath "$1")
migration=ops/sql/20260716_memory_v1_owner_self_v5.sql
test_sql=tests/memory_v1_owner_self_v5.sql
required_ancestor=88b134c93ca3fb4cab5a35435ab18865646fd1a0
expected_migration_sha=8ad5b7d03ed9a0d3f888c24cc5d6b7f7d1421d8413e84ce240e7baa6a9e5c168
expected_test_sha=1a43cb581db51d57306706b11fe619d40c104ca5c88e98a920ab9f7cc3b686d0
container=brains-postgres-1
database=memory
database_role=sage
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_owner_self_v5_install.lock
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
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]] || {
  echo "owner-self migration hash mismatch" >&2
  exit 1
}
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]] || {
  echo "owner-self test hash mismatch" >&2
  exit 1
}

validate_authorization() {
  AUTHORIZATION="$authorization" EXPECTED_HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
    python3 - <<'PY'
import datetime as dt
import json
import os
import stat
import uuid
from pathlib import Path

path = Path(os.environ["AUTHORIZATION"])
if stat.S_IMODE(path.stat().st_mode) != 0o600:
    raise SystemExit("authorization file mode must be 0600")
value = json.loads(path.read_text(encoding="utf-8"))
expected = {
    "contract_version", "authorization_id", "authorized", "authorized_by",
    "authorized_at", "expires_at", "expected_head_commit", "target_server",
    "scope",
}
if set(value) != expected:
    raise SystemExit("authorization keys do not exactly match the contract")
if value["contract_version"] != "memory_v1_owner_self_v5_production_install_authorization_v1":
    raise SystemExit("authorization contract mismatch")
if value["authorized"] is not True or value["authorized_by"] != "Eric Lund":
    raise SystemExit("authorization identity or flag mismatch")
uuid.UUID(value["authorization_id"])
if value["expected_head_commit"] != os.environ["EXPECTED_HEAD"]:
    raise SystemExit("authorization commit mismatch")
if value["target_server"] != "seebx":
    raise SystemExit("authorization server mismatch")
if value["scope"] != "schema_functions_and_rolled_back_security_test_only":
    raise SystemExit("authorization scope mismatch")
issued = dt.datetime.fromisoformat(value["authorized_at"].replace("Z", "+00:00"))
expires = dt.datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
now = dt.datetime.now(dt.timezone.utc)
if issued.utcoffset() != dt.timedelta(0) or expires.utcoffset() != dt.timedelta(0):
    raise SystemExit("authorization timestamps must use UTC")
if expires <= issued or expires - issued > dt.timedelta(minutes=30):
    raise SystemExit("authorization validity window is invalid")
if now < issued - dt.timedelta(seconds=30) or now >= expires:
    raise SystemExit("authorization is not currently valid")
PY
}

validate_authorization
exec 9>"$lock_file"
flock -n 9 || {
  echo "another owner-self V5 installation holds $lock_file" >&2
  exit 1
}
umask 077

auth_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' "$authorization")
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_owner_self_v5_install_${run_id}.status"

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

phase=preflight
[[ "$(psql_scalar "SELECT current_setting('server_version_num')::integer / 10000")" == "16" ]] || {
  echo "PostgreSQL major mismatch" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT to_regclass('memory.entity') IS NOT NULL AND to_regprocedure('memory.require_v5_writer_context()') IS NOT NULL")" == "t" ]] || {
  echo "owner-self prerequisites are missing" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT to_regclass('memory.owner_self_bootstrap_v5') IS NULL AND to_regprocedure('memory.preflight_owner_self_v5()') IS NULL AND to_regprocedure('memory.bootstrap_owner_self_v5(uuid,text)') IS NULL")" == "t" ]] || {
  echo "owner-self V5 objects are not absent" >&2
  exit 1
}
database_size=$(psql_scalar "SELECT pg_database_size(current_database())")
available_kb=$(df -Pk "$snapshot_dir" | awk 'NR==2 {print $4}')
(( available_kb * 1024 >= database_size * 2 )) || {
  echo "backup volume has less than twice the database size free" >&2
  exit 1
}

phase=baseline_capture
baseline_tables="$snapshot_dir/memory_v1_owner_self_v5_baseline_tables_${run_id}.txt"
baseline_state="$snapshot_dir/memory_v1_owner_self_v5_baseline_state_${run_id}.tsv"
post_state="$snapshot_dir/memory_v1_owner_self_v5_post_state_${run_id}.tsv"
psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" >"$baseline_tables"
: >"$baseline_state"
while IFS= read -r table; do
  [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || { echo "unsafe table name: $table" >&2; exit 1; }
  state=$(psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text AS row_json FROM memory.\"$table\" AS t) AS rows")
  printf '%s\t%s\n' "$table" "$state" >>"$baseline_state"
done <"$baseline_tables"
chmod 0600 "$baseline_tables" "$baseline_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_owner_self_v5_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_owner_self_v5_${run_id}.dump"
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
validate_authorization

phase=schema_install
install_log="$snapshot_dir/memory_v1_owner_self_v5_install_${run_id}.log"
: >"$install_log"
run_sql_file "$migration" >>"$install_log" 2>&1

phase=rolled_back_security_test
run_sql_file "$test_sql" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=postflight
: >"$post_state"
while IFS= read -r table; do
  state=$(psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text AS row_json FROM memory.\"$table\" AS t) AS rows")
  printf '%s\t%s\n' "$table" "$state" >>"$post_state"
done <"$baseline_tables"
chmod 0600 "$post_state"
cmp -s "$baseline_state" "$post_state" || {
  diff -u "$baseline_state" "$post_state" >&2 || true
  echo "preexisting Memory V1 rows changed" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT count(*) FROM memory.owner_self_bootstrap_v5")" == "0" ]] || {
  echo "owner-self audit table is not empty" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity WHERE entity_type='self'")" == "0" ]] || {
  echo "live owner-self rows unexpectedly exist" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT has_function_privilege('brains_app','memory.preflight_owner_self_v5()','EXECUTE') AND has_function_privilege('brains_app','memory.bootstrap_owner_self_v5(uuid,text)','EXECUTE')")" == "t" ]] || {
  echo "brains_app controlled API grants are missing" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid='memory.owner_self_bootstrap_v5'::regclass) AND (SELECT pg_get_userbyid(proowner)='memory_v5_writer' FROM pg_proc WHERE oid='memory.preflight_owner_self_v5()'::regprocedure) AND (SELECT pg_get_userbyid(proowner)='memory_v5_writer' FROM pg_proc WHERE oid='memory.bootstrap_owner_self_v5(uuid,text)'::regprocedure) AND (SELECT indisunique FROM pg_index WHERE indexrelid='memory.entity_one_active_self_v5'::regclass)")" == "t" ]] || {
  echo "owner-self RLS, function ownership, or unique-index contract mismatch" >&2
  exit 1
}
for privilege in SELECT INSERT UPDATE DELETE TRUNCATE REFERENCES TRIGGER; do
  [[ "$(psql_scalar "SELECT has_table_privilege('brains_app','memory.owner_self_bootstrap_v5','$privilege')")" == "f" ]] || {
    echo "brains_app unexpectedly has $privilege on owner-self audit" >&2
    exit 1
  }
done

phase=report
report="$snapshot_dir/memory_v1_owner_self_v5_install_${run_id}.json"
AUTHORIZATION="$authorization" BACKUP="$backup" CATALOG="$catalog" \
  CHECKSUM="$checksum" INSTALL_LOG="$install_log" BASELINE="$baseline_state" \
  POST="$post_state" REPORT="$report" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
  python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

authorization = json.loads(Path(os.environ["AUTHORIZATION"]).read_text())
report = {
    "contract_version": "memory_v1_owner_self_v5_production_install_report_v1",
    "authorization_id": authorization["authorization_id"],
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "target": {"server": "seebx", "database": "memory"},
    "backup": {
        "path": os.environ["BACKUP"],
        "restore_catalog": os.environ["CATALOG"],
        "sha256_file": os.environ["CHECKSUM"],
    },
    "evidence": {
        "install_log": os.environ["INSTALL_LOG"],
        "baseline_state": os.environ["BASELINE"],
        "post_state": os.environ["POST"],
    },
    "checks": {
        "schema_and_functions_installed": True,
        "security_test_rolled_back": True,
        "preexisting_rows_and_content_unchanged": True,
        "owner_self_audit_rows": 0,
        "live_owner_self_entities": 0,
        "runtime_extraction_or_retrieval_invoked": False,
        "qdrant_or_redis_write_invoked": False,
    },
    "hard_stop": "before_live_owner_self_bootstrap",
}
Path(os.environ["REPORT"]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_owner_self_v5_production_install: PASS\n'
printf 'report=%s\n' "$report"
printf 'backup=%s\n' "$backup"
