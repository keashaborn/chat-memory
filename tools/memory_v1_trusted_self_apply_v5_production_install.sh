#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs one exact function-body compatibility patch and
# executes the staged self-resolution test inside rollback. It does not review
# or durably apply any entity resolution.

if [[ $# -ne 1 ]]; then
  echo "usage: $0 AUTHORIZATION.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_TRUSTED_SELF_APPLY_INSTALL:-}" != "authorized" ]]; then
  echo "MEMORY_V1_TRUSTED_SELF_APPLY_INSTALL=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
authorization=$(realpath "$1")
migration=ops/sql/20260716_memory_v1_trusted_self_apply_v5.sql
test_sql=tests/memory_v1_trusted_self_apply_v5.sql
required_ancestor=ca97890cccbf35a6ba22783ccad2cf1341c3619c
expected_migration_sha=d44573b7b0da10791680b1870abd34ea476c056cc4b85df7219b87daf4581de5
expected_test_sha=64a853c8b6d52197e7e3d630302726501763bb455e44d0034fb415ae3049d9de
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
resolution=fc1859aa-f280-4aa1-b75a-ee882e969c87
self_entity=35029129-27bd-457b-8cb5-82dd37ba32ba
test_request=30000000-0000-4000-8000-000000000001
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_trusted_self_apply_v5_install.lock
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
    "contract_version", "authorization_id", "authorized", "authorized_by",
    "authorized_at", "expires_at", "expected_head_commit", "target_server",
    "scope", "migration_sha256", "test_sha256",
}
if set(value) != expected:
    raise SystemExit("authorization keys do not exactly match the contract")
if value["contract_version"] != "memory_v1_trusted_self_apply_v5_install_authorization_v1":
    raise SystemExit("authorization contract mismatch")
if value["authorized"] is not True or value["authorized_by"] != "Eric Lund":
    raise SystemExit("authorization identity or flag mismatch")
uuid.UUID(value["authorization_id"])
if value["expected_head_commit"] != os.environ["EXPECTED_HEAD"]:
    raise SystemExit("authorization commit mismatch")
if value["target_server"] != "seebx":
    raise SystemExit("authorization server mismatch")
if value["scope"] != "install_trusted_self_apply_compat_and_rollback_only_test":
    raise SystemExit("authorization scope mismatch")
if value["migration_sha256"] != "d44573b7b0da10791680b1870abd34ea476c056cc4b85df7219b87daf4581de5":
    raise SystemExit("authorization migration hash mismatch")
if value["test_sha256"] != "64a853c8b6d52197e7e3d630302726501763bb455e44d0034fb415ae3049d9de":
    raise SystemExit("authorization test hash mismatch")
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
flock -n 9 || { echo "another trusted-self install holds the lock" >&2; exit 1; }
umask 077
auth_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' "$authorization")
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_trusted_self_apply_v5_install_${run_id}.status"

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

phase=preflight
definition=$(psql_scalar "SELECT pg_get_functiondef('memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)'::regprocedure)")
[[ "$definition" == *"entity_row.entity_key <> 'self'"* && "$definition" != *"trusted_owner_self"* ]] || {
  echo "production apply function is not the expected strict predecessor" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid='memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)'::regprocedure")" == "memory_v5_writer" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_plan WHERE owner_user_id='$owner'::uuid AND resolution_id='$resolution'::uuid AND action='link_existing' AND decision_state='auto_link_eligible' AND selected_entity_id='$self_entity'::uuid")" == "1" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid AND entity_id='$self_entity'::uuid AND entity_type='self' AND status='active' AND metadata->>'identity_state'='trusted_owner_self'")" == "1" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_apply WHERE resolution_id='$resolution'::uuid")" == "0" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.relational_operation_request WHERE request_id='$test_request'::uuid")" == "0" ]]

phase=baseline_capture
baseline_tables="$snapshot_dir/memory_v1_trusted_self_apply_tables_${run_id}.txt"
baseline_state="$snapshot_dir/memory_v1_trusted_self_apply_baseline_${run_id}.tsv"
post_state="$snapshot_dir/memory_v1_trusted_self_apply_post_${run_id}.tsv"
psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" >"$baseline_tables"
: >"$baseline_state"
while IFS= read -r table; do
  [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || exit 1
  state=$(psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text row_json FROM memory.\"$table\" t) rows")
  printf '%s\t%s\n' "$table" "$state" >>"$baseline_state"
done <"$baseline_tables"
qdrant_before=$(qdrant_signature)
chmod 0600 "$baseline_tables" "$baseline_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_trusted_self_apply_v5_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_trusted_self_apply_v5_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"
validate_authorization

phase=schema_install
install_log="$snapshot_dir/memory_v1_trusted_self_apply_v5_install_${run_id}.log"
run_sql_file "$migration" >"$install_log" 2>&1

phase=rolled_back_security_test
run_sql_file "$test_sql" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=postflight
: >"$post_state"
while IFS= read -r table; do
  state=$(psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text row_json FROM memory.\"$table\" t) rows")
  printf '%s\t%s\n' "$table" "$state" >>"$post_state"
done <"$baseline_tables"
chmod 0600 "$post_state"
cmp -s "$baseline_state" "$post_state" || {
  diff -u "$baseline_state" "$post_state" >&2 || true
  echo "Memory V1 row state changed during trusted-self compatibility install" >&2
  exit 1
}
definition=$(psql_scalar "SELECT pg_get_functiondef('memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)'::regprocedure)")
[[ "$definition" == *"trusted_owner_self"* && "$definition" != *"entity_row.entity_key <> 'self'"* ]] || {
  echo "trusted-self apply compatibility body is missing" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid='memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)'::regprocedure")" == "memory_v5_writer" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo "Qdrant changed during trusted-self compatibility install" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_trusted_self_apply_v5_install_${run_id}.json"
AUTHORIZATION="$authorization" BACKUP="$backup" CATALOG="$catalog" \
  BASELINE="$baseline_state" POST="$post_state" LOG="$install_log" \
  REPORT="$report" HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
  QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

auth = json.loads(Path(os.environ["AUTHORIZATION"]).read_text())
value = {
    "contract_version": "memory_v1_trusted_self_apply_v5_install_report_v1",
    "authorization_id": auth["authorization_id"],
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
        "trusted_owner_self_target_accepted": True,
        "cross_owner_preflight_rejected": True,
        "apply_and_replay_test_rolled_back": True,
        "memory_rows_and_content_unchanged": True,
        "restricted_function_owner_preserved": True,
        "qdrant_unchanged": True,
        "durable_resolution_review_or_apply_invoked": False,
    },
    "hard_stop": "before_entity_resolution_review_apply_or_projection",
}
Path(os.environ["REPORT"]).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_trusted_self_apply_v5_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
