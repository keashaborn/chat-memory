#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Creates exactly one trusted owner-self entity plus one
# append-only audit row through the controlled database function, then proves
# request replay is zero-write. No extraction, projection, or retrieval runs.

if [[ $# -ne 1 ]]; then
  echo "usage: $0 AUTHORIZATION.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_OWNER_SELF_APPLY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_OWNER_SELF_APPLY=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
authorization=$(realpath "$1")
required_ancestor=88b134c93ca3fb4cab5a35435ab18865646fd1a0
container=brains-postgres-1
database=memory
database_role=sage
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_owner_self_v5_apply.lock
phase=initialization
status_file=

if [[ "$authorization" == "$repo_root"/* ]]; then
  echo "authorization file must be outside the Git worktree" >&2
  exit 1
fi
if [[ -n "$(git -C "$repo_root" status --porcelain)" ]]; then
  echo "owner-self apply requires a clean Git worktree" >&2
  exit 1
fi
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD

validate_authorization() {
  AUTHORIZATION="$authorization" EXPECTED_HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
    python3 - <<'PY'
import datetime as dt
import json
import os
import re
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
    "scope", "owner_user_id", "request_id", "preflight_manifest_sha256",
}
if set(value) != expected:
    raise SystemExit("authorization keys do not exactly match the contract")
if value["contract_version"] != "memory_v1_owner_self_v5_apply_authorization_v1":
    raise SystemExit("authorization contract mismatch")
if value["authorized"] is not True or value["authorized_by"] != "Eric Lund":
    raise SystemExit("authorization identity or flag mismatch")
uuid.UUID(value["authorization_id"])
uuid.UUID(value["owner_user_id"])
uuid.UUID(value["request_id"])
if not re.fullmatch(r"[0-9a-f]{64}", value["preflight_manifest_sha256"]):
    raise SystemExit("preflight manifest is invalid")
if value["expected_head_commit"] != os.environ["EXPECTED_HEAD"]:
    raise SystemExit("authorization commit mismatch")
if value["target_server"] != "seebx":
    raise SystemExit("authorization server mismatch")
if value["scope"] != "create_one_owner_self_entity_and_audit_then_zero_write_replay":
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
owner=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["owner_user_id"])' "$authorization")
request_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["request_id"])' "$authorization")
expected_manifest=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["preflight_manifest_sha256"])' "$authorization")
auth_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' "$authorization")

exec 9>"$lock_file"
flock -n 9 || {
  echo "another owner-self V5 apply holds $lock_file" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_owner_self_v5_apply_${run_id}.status"

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

actor_query() {
  local sql=$1
  docker exec -i "$container" psql -X -A -t -F '|' -v ON_ERROR_STOP=1 \
    -U "$database_role" -d "$database" <<SQL
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$owner',true) \gset
$sql
RESET SESSION AUTHORIZATION;
COMMIT;
SQL
}

preflight() {
  actor_query "SELECT state,coalesce(existing_entity_id::text,''),prior_state_sha256,authorization_manifest_sha256 FROM memory.preflight_owner_self_v5();" \
    | sed '/^BEGIN$/d;/^SET$/d;/^RESET$/d;/^COMMIT$/d;/^[[:space:]]*$/d'
}

qdrant_signature() {
  curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id)' | sha256sum | awk '{print $1}'
}

phase=preflight
[[ "$(psql_scalar "SELECT to_regprocedure('memory.bootstrap_owner_self_v5(uuid,text)') IS NOT NULL")" == "t" ]] || {
  echo "owner-self controlled function is missing" >&2
  exit 1
}
before_preflight=$(preflight)
IFS='|' read -r before_state before_entity before_state_sha before_manifest <<<"$before_preflight"
[[ "$before_state" == "create" && -z "$before_entity" ]] || {
  echo "owner-self preflight is not create/absent" >&2
  exit 1
}
[[ "$before_manifest" == "$expected_manifest" ]] || {
  echo "authorized owner-self manifest is stale" >&2
  exit 1
}

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_owner_self_v5_apply_baseline_${run_id}.json"
other_owner_entity_sha=$(psql_scalar "SELECT encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(e)::text AS row_json FROM memory.entity AS e WHERE owner_user_id <> '$owner'::uuid) AS rows")
target_nonself_sha=$(psql_scalar "SELECT encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(e)::text AS row_json FROM memory.entity AS e WHERE owner_user_id='$owner'::uuid AND entity_type <> 'self') AS rows")
before_entity_count=$(psql_scalar "SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid")
before_audit_count=$(psql_scalar "SELECT count(*) FROM memory.owner_self_bootstrap_v5 WHERE owner_user_id='$owner'::uuid")
baseline_qdrant_sha=$(qdrant_signature)
OWNER="$owner" STATE_SHA="$before_state_sha" MANIFEST="$before_manifest" \
  ENTITY_COUNT="$before_entity_count" AUDIT_COUNT="$before_audit_count" \
  OTHER_SHA="$other_owner_entity_sha" NONSELF_SHA="$target_nonself_sha" \
  QDRANT_SHA="$baseline_qdrant_sha" OUTPUT="$baseline" python3 - <<'PY'
import json, os
from pathlib import Path
value = {
    "owner_user_id": os.environ["OWNER"],
    "prior_state_sha256": os.environ["STATE_SHA"],
    "authorization_manifest_sha256": os.environ["MANIFEST"],
    "owner_entity_count": int(os.environ["ENTITY_COUNT"]),
    "owner_audit_count": int(os.environ["AUDIT_COUNT"]),
    "other_owner_entity_sha256": os.environ["OTHER_SHA"],
    "owner_nonself_entity_sha256": os.environ["NONSELF_SHA"],
    "qdrant_sha256": os.environ["QDRANT_SHA"],
}
Path(os.environ["OUTPUT"]).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
PY
chmod 0600 "$baseline"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_owner_self_apply_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_owner_self_apply_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U "$database_role" -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]] || { echo "backup is empty" >&2; exit 1; }
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]] || { echo "backup restore catalog is empty" >&2; exit 1; }
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"
validate_authorization
[[ "$(preflight)" == "$before_preflight" ]] || {
  echo "owner-self preflight changed during backup" >&2
  exit 1
}
before_qdrant_sha=$(qdrant_signature)

phase=transactional_apply
apply_output=$(actor_query "SELECT entity_id,outcome,rows_written FROM memory.bootstrap_owner_self_v5('$request_id'::uuid,'$expected_manifest');" \
  | sed '/^BEGIN$/d;/^SET$/d;/^RESET$/d;/^COMMIT$/d;/^[[:space:]]*$/d')
IFS='|' read -r created_entity outcome rows_written <<<"$apply_output"
[[ "$outcome" == "created" && "$rows_written" == "2" ]] || {
  echo "owner-self apply result mismatch" >&2
  exit 1
}

phase=zero_write_replay
replay_output=$(actor_query "SELECT entity_id,outcome,rows_written FROM memory.bootstrap_owner_self_v5('$request_id'::uuid,'$expected_manifest');" \
  | sed '/^BEGIN$/d;/^SET$/d;/^RESET$/d;/^COMMIT$/d;/^[[:space:]]*$/d')
IFS='|' read -r replay_entity replay_outcome replay_rows <<<"$replay_output"
[[ "$replay_entity" == "$created_entity" && "$replay_outcome" == "replayed" && "$replay_rows" == "0" ]] || {
  echo "owner-self replay result mismatch" >&2
  exit 1
}

phase=postflight
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid")" == "$((before_entity_count + 1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.owner_self_bootstrap_v5 WHERE owner_user_id='$owner'::uuid")" == "$((before_audit_count + 1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid AND entity_type='self' AND status='active' AND entity_id='$created_entity'::uuid AND canonical_name='Self' AND normalized_name='self' AND entity_key !~ '$owner'")" == "1" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.owner_self_bootstrap_v5 WHERE owner_user_id='$owner'::uuid AND request_id='$request_id'::uuid AND manifest_sha256='$expected_manifest' AND entity_id='$created_entity'::uuid")" == "1" ]]
[[ "$(psql_scalar "SELECT encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(e)::text AS row_json FROM memory.entity AS e WHERE owner_user_id <> '$owner'::uuid) AS rows")" == "$other_owner_entity_sha" ]]
[[ "$(psql_scalar "SELECT encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(e)::text AS row_json FROM memory.entity AS e WHERE owner_user_id='$owner'::uuid AND entity_type <> 'self') AS rows")" == "$target_nonself_sha" ]]
after_qdrant_sha=$(qdrant_signature)
[[ "$after_qdrant_sha" == "$before_qdrant_sha" ]] || {
  echo "Qdrant changed during owner-self apply" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_owner_self_v5_apply_${run_id}.json"
AUTHORIZATION="$authorization" BASELINE="$baseline" BACKUP="$backup" \
  CATALOG="$catalog" CREATED_ENTITY="$created_entity" REPORT="$report" \
  QDRANT_BEFORE="$before_qdrant_sha" QDRANT_AFTER="$after_qdrant_sha" \
  HEAD="$(git -C "$repo_root" rev-parse HEAD)" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

authorization = json.loads(Path(os.environ["AUTHORIZATION"]).read_text())
report = {
    "contract_version": "memory_v1_owner_self_v5_apply_report_v1",
    "authorization_id": authorization["authorization_id"],
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": authorization["owner_user_id"],
    "request_id": authorization["request_id"],
    "created_entity_id": os.environ["CREATED_ENTITY"],
    "backup": {
        "path": os.environ["BACKUP"],
        "restore_catalog": os.environ["CATALOG"],
        "sha256_file": os.environ["BACKUP"] + ".sha256",
    },
    "evidence": {
        "baseline": os.environ["BASELINE"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "exact_durable_rows_created": 2,
        "self_entity_rows_created": 1,
        "append_only_audit_rows_created": 1,
        "replay_rows_written": 0,
        "other_owner_entities_unchanged": True,
        "owner_nonself_entities_unchanged": True,
        "qdrant_unchanged": True,
        "extraction_projection_or_retrieval_invoked": False,
    },
    "hard_stop": "before_v5_stage_or_projection_apply",
}
Path(os.environ["REPORT"]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_owner_self_v5_apply: PASS\n'
printf 'created_entity_id=%s\n' "$created_entity"
printf 'report=%s\n' "$report"
printf 'backup=%s\n' "$backup"
