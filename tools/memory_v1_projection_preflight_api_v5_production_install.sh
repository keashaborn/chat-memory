#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs two owner-scoped read-only preflight APIs.
# It writes no Memory V1 rows and does not stage or apply a projection.

if [[ $# -ne 1 ]]; then
  echo "usage: $0 AUTHORIZATION.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_PROJECTION_PREFLIGHT_API_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_PROJECTION_PREFLIGHT_API_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
authorization=$(realpath "$1")
migration=ops/sql/20260716_memory_v1_projection_preflight_api_v5.sql
test_sql=tests/memory_v1_projection_preflight_api_v5.sql
required_ancestor=7c18e22c2781f2acba9d6909a572a47d6d840234
expected_migration_sha=b1dc7fa87ef3af5c3df918ac0369f836020a048bdc636c651d09f70a4cd43e14
expected_test_sha=2acf45a368187820052b749cb8fff576597d5048ca457901c873a8e48459f928
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_projection_preflight_api_install.lock
phase=initialization
status_file=

if [[ "$authorization" == "$repo_root"/* ]]; then
  echo "authorization file must be outside the Git worktree" >&2
  exit 1
fi
[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
  echo "production install requires a clean Git worktree" >&2
  exit 1
}
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]

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
value = json.loads(path.read_text())
expected = {
    "contract_version", "authorization_id", "phase_authorization",
    "authorized", "authorized_by", "authorized_at", "expires_at",
    "expected_head_commit", "target_server", "scope",
    "migration_sha256", "test_sha256",
}
if set(value) != expected:
    raise SystemExit("authorization keys do not exactly match the contract")
if value["contract_version"] != "memory_v1_projection_preflight_api_v5_install_authorization_v1":
    raise SystemExit("authorization contract mismatch")
if value["phase_authorization"] != "memory_v1_v5_entity_resolution_projection_preparation_20260716":
    raise SystemExit("phase authorization mismatch")
if value["authorized"] is not True or value["authorized_by"] != "Eric Lund":
    raise SystemExit("authorization identity or flag mismatch")
uuid.UUID(value["authorization_id"])
if value["expected_head_commit"] != os.environ["EXPECTED_HEAD"]:
    raise SystemExit("authorization commit mismatch")
exact = {
    "target_server": "seebx",
    "scope": "install_owner_scoped_projection_preflight_api",
    "migration_sha256": "b1dc7fa87ef3af5c3df918ac0369f836020a048bdc636c651d09f70a4cd43e14",
    "test_sha256": "2acf45a368187820052b749cb8fff576597d5048ca457901c873a8e48459f928",
}
for key, expected_value in exact.items():
    if value[key] != expected_value:
        raise SystemExit(f"authorization {key} mismatch")
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
flock -n 9 || { echo "another projection preflight install holds the lock" >&2; exit 1; }
umask 077
auth_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' "$authorization")
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_projection_preflight_api_install_${run_id}.status"

record_exit() {
  exit_code=$?
  {
    printf 'run_id=%s\n' "$run_id"
    printf 'phase=%s\n' "$phase"
    printf 'exit_code=%s\n' "$exit_code"
    printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$status_file"
  chmod 0600 "$status_file"
}
trap record_exit EXIT

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

run_sql_file() {
  docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=120s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
    <"$repo_root/$1"
}

qdrant_signature() {
  curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id)' | sha256sum | awk '{print $1}'
}

capture_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || exit 1
    state=$(psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text row_json FROM memory.\"$table\" t) rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "SELECT (to_regprocedure('memory.preflight_projection_source_v5(uuid)') IS NULL AND to_regprocedure('memory.preflight_projection_packet_v5(uuid,text)') IS NULL)::int")" == "1" ]]

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_projection_preflight_api_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_projection_preflight_api_post_${run_id}.tsv"
capture_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=backup
backup_partial="$snapshot_dir/.memory_pre_projection_preflight_api_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_projection_preflight_api_${run_id}.dump"
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
validate_authorization

phase=schema_install
install_log="$snapshot_dir/memory_v1_projection_preflight_api_install_${run_id}.log"
run_sql_file "$migration" >"$install_log" 2>&1

phase=read_only_security_test
run_sql_file "$test_sql" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=postflight
capture_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo "Memory V1 row state changed during preflight API install" >&2
  exit 1
}
owner_state=$(psql_scalar "
  SELECT
    pg_get_userbyid((SELECT proowner FROM pg_proc
      WHERE oid='memory.preflight_projection_source_v5(uuid)'::regprocedure))
    || '|' ||
    pg_get_userbyid((SELECT proowner FROM pg_proc
      WHERE oid='memory.preflight_projection_packet_v5(uuid,text)'::regprocedure))
")
[[ "$owner_state" == 'memory_v5_writer|memory_v5_writer' ]]
[[ "$(psql_scalar "SELECT has_function_privilege('brains_app','memory.preflight_projection_source_v5(uuid)','EXECUTE')::int || '|' || has_function_privilege('brains_app','memory.preflight_projection_packet_v5(uuid,text)','EXECUTE')::int")" == "1|1" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo "Qdrant changed during projection preflight API install" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_projection_preflight_api_install_${run_id}.json"
AUTHORIZATION="$authorization" BACKUP="$backup" CATALOG="$catalog" \
  BASELINE="$baseline" POST="$post" LOG="$install_log" REPORT="$report" \
  QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
  HEAD="$(git -C "$repo_root" rev-parse HEAD)" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

auth = json.loads(Path(os.environ["AUTHORIZATION"]).read_text())
value = {
    "contract_version": "memory_v1_projection_preflight_api_v5_install_report_v1",
    "authorization_id": auth["authorization_id"],
    "phase_authorization": auth["phase_authorization"],
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "evidence": {
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "log": os.environ["LOG"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "source_api_owner_scoped": True,
        "packet_api_owner_bound": True,
        "cross_owner_rejected": True,
        "restricted_function_owner_preserved": True,
        "memory_rows_and_content_unchanged": True,
        "qdrant_unchanged": True,
        "projection_staging_or_apply_invoked": False,
    },
    "hard_stop": "before_projection_packet_generation_or_staging",
}
Path(os.environ["REPORT"]).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_projection_preflight_api_v5_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
