#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies one approved create-new entity resolution. It
# does not create claims/projections, write Qdrant, or activate retrieval.

if [[ $# -ne 1 ]]; then
  echo "usage: $0 AUTHORIZATION.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_TRAINER_RESOLUTION_APPLY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_TRAINER_RESOLUTION_APPLY=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
authorization=$(realpath "$1")
required_ancestor=764097a6af5cab86871ed0679bb54e08d7cfabad
test_sql=tests/memory_v1_v5_trainer_resolution_apply.sql
expected_test_sha=4e35a2578319a6888b2cb818615671c73b3951e22a35cef503edaa0b99f32b41
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
resolution=a8befb71-413b-49b0-a460-c683ea52038e
review=03f72707-bd55-4179-8908-4ef204db7309
request=32000000-0000-4000-8000-000000000001
manifest=ad5a31b81d4081583f95bc29a85fcf7de070d59aa535b09e6f3ad9310db53558
observation=9bf1e6b2-1840-4524-98dc-142567ebe013
self_entity=35029129-27bd-457b-8cb5-82dd37ba32ba
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_trainer_resolution_apply.lock
phase=initialization
status_file=

if [[ "$authorization" == "$repo_root"/* ]]; then
  echo "authorization file must be outside the Git worktree" >&2
  exit 1
fi
[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
  echo "production apply requires a clean Git worktree" >&2
  exit 1
}
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
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
    "expected_head_commit", "target_server", "scope", "owner_user_id",
    "resolution_id", "review_id", "request_id", "apply_manifest_sha256",
    "expected_rows_created", "test_sha256",
}
if set(value) != expected:
    raise SystemExit("authorization keys do not exactly match the contract")
if value["contract_version"] != "memory_v1_v5_trainer_resolution_apply_authorization_v1":
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
    "scope": "apply_approved_personal_trainer_resolution",
    "owner_user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
    "resolution_id": "a8befb71-413b-49b0-a460-c683ea52038e",
    "review_id": "03f72707-bd55-4179-8908-4ef204db7309",
    "request_id": "32000000-0000-4000-8000-000000000001",
    "apply_manifest_sha256": "ad5a31b81d4081583f95bc29a85fcf7de070d59aa535b09e6f3ad9310db53558",
    "expected_rows_created": 5,
    "test_sha256": "4e35a2578319a6888b2cb818615671c73b3951e22a35cef503edaa0b99f32b41",
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
flock -n 9 || { echo "another trainer resolution apply holds the lock" >&2; exit 1; }
umask 077
auth_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' "$authorization")
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_trainer_resolution_apply_${run_id}.status"

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

actor_scalar() {
  local sql=$1
  docker exec -i "$container" psql -X -q -A -t -F '|' \
    -v ON_ERROR_STOP=1 -U sage -d "$database" <<SQL
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$owner',true) \gset
$sql
RESET SESSION AUTHORIZATION;
ROLLBACK;
SQL
}

qdrant_signature() {
  curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id)' | sha256sum | awk '{print $1}'
}

table_state() {
  local table=$1
  local predicate=${2:-true}
  [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || exit 1
  psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text row_json FROM memory.\"$table\" t WHERE $predicate) rows"
}

capture_non_target_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    printf '%s\t%s\n' "$table" "$(table_state "$table")" >>"$output"
  done < <(psql_scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory'
      AND table_type='BASE TABLE'
      AND table_name NOT IN (
        'entity', 'entity_resolution_apply', 'entity_alias_observation',
        'observation_entity_binding', 'relational_operation_request'
      )
    ORDER BY table_name
  ")
  printf 'entity_other\t%s\n' \
    "$(table_state entity "COALESCE(metadata->>'resolution_id','') <> '$resolution'")" \
    >>"$output"
  printf 'entity_resolution_apply_other\t%s\n' \
    "$(table_state entity_resolution_apply "resolution_id <> '$resolution'::uuid")" \
    >>"$output"
  printf 'entity_alias_observation_other\t%s\n' \
    "$(table_state entity_alias_observation "resolution_id <> '$resolution'::uuid")" \
    >>"$output"
  printf 'observation_entity_binding_other\t%s\n' \
    "$(table_state observation_entity_binding "observation_id <> '$observation'::uuid")" \
    >>"$output"
  printf 'relational_operation_request_other\t%s\n' \
    "$(table_state relational_operation_request "request_id <> '$request'::uuid")" \
    >>"$output"
  chmod 0600 "$output"
}

capture_all_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    printf '%s\t%s\n' "$table" "$(table_state "$table")" >>"$output"
  done < <(psql_scalar "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

current_preflight() {
  actor_scalar "
    SELECT resolution_id,action,decision_state,review_id,
           coalesce(prospective_entity_id::text,''),entity_state_sha256,
           apply_manifest_sha256
      FROM memory.preflight_entity_resolution_apply_v5(
        '$resolution'::uuid,'$review'::uuid
      );
  "
}

phase=preflight
expected_preflight="$resolution|create_new|manual_review_required|$review||26e85d5b3864079186ac2c8c901e37785faf28ef6e851bf419a2d4b6172cc16e|$manifest"
[[ "$(current_preflight)" == "$expected_preflight" ]] || {
  echo "trainer resolution preflight drifted" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_apply WHERE resolution_id='$resolution'::uuid")" == "0" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity WHERE metadata->>'resolution_id'='$resolution'")" == "0" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_alias_observation WHERE resolution_id='$resolution'::uuid")" == "0" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.observation_entity_binding WHERE observation_id='$observation'::uuid")" == "0" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.relational_operation_request WHERE request_id='$request'::uuid")" == "0" ]]

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_trainer_resolution_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_trainer_resolution_post_${run_id}.tsv"
capture_non_target_state "$baseline"
qdrant_before=$(qdrant_signature)
before_entity_count=$(psql_scalar "SELECT count(*) FROM memory.entity")
before_apply_count=$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_apply")
before_alias_count=$(psql_scalar "SELECT count(*) FROM memory.entity_alias_observation")
before_binding_count=$(psql_scalar "SELECT count(*) FROM memory.observation_entity_binding")
before_request_count=$(psql_scalar "SELECT count(*) FROM memory.relational_operation_request")

phase=backup
backup_partial="$snapshot_dir/.memory_pre_trainer_resolution_apply_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_trainer_resolution_apply_${run_id}.dump"
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
[[ "$(current_preflight)" == "$expected_preflight" ]] || {
  echo "trainer resolution preflight changed during backup" >&2
  exit 1
}

phase=transactional_apply
apply_output=$(docker exec -i "$container" psql -X -q -A -t -F '|' \
  -v ON_ERROR_STOP=1 -U sage -d "$database" <<SQL
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$owner',true) \gset
SELECT * FROM memory.apply_entity_resolution_v5(
  '$request'::uuid,'$resolution'::uuid,'$review'::uuid,'$manifest'
) \gset trainer_
SELECT 1 / ((:'trainer_outcome'='applied')::integer);
SELECT 1 / ((:'trainer_bindings_created'::integer=1)::integer);
SELECT :'trainer_applied_entity_id',:'trainer_outcome',:'trainer_bindings_created';
RESET SESSION AUTHORIZATION;
COMMIT;
SQL
)
apply_output=$(printf '%s\n' "$apply_output" | sed '/^1$/d;/^[[:space:]]*$/d')
IFS='|' read -r entity_id apply_outcome bindings_created <<<"$apply_output"
[[ "$entity_id" =~ ^[0-9a-f-]{36}$ ]]
[[ "$apply_outcome" == "applied" && "$bindings_created" == "1" ]]

phase=zero_write_replay
replay_before="$snapshot_dir/memory_v1_trainer_resolution_replay_before_${run_id}.tsv"
replay_after="$snapshot_dir/memory_v1_trainer_resolution_replay_after_${run_id}.tsv"
capture_all_state "$replay_before"
replay_output=$(docker exec -i "$container" psql -X -q -A -t -F '|' \
  -v ON_ERROR_STOP=1 -U sage -d "$database" <<SQL
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$owner',true) \gset
SELECT * FROM memory.apply_entity_resolution_v5(
  '$request'::uuid,'$resolution'::uuid,'$review'::uuid,'$manifest'
) \gset trainer_
SELECT 1 / ((:'trainer_outcome'='replayed')::integer);
SELECT 1 / ((:'trainer_bindings_created'::integer=0)::integer);
SELECT :'trainer_applied_entity_id',:'trainer_outcome',:'trainer_bindings_created';
RESET SESSION AUTHORIZATION;
COMMIT;
SQL
)
replay_output=$(printf '%s\n' "$replay_output" | sed '/^1$/d;/^[[:space:]]*$/d')
IFS='|' read -r replay_entity replay_outcome replay_bindings <<<"$replay_output"
[[ "$replay_entity" == "$entity_id" && "$replay_outcome" == "replayed" ]]
[[ "$replay_bindings" == "0" ]]
capture_all_state "$replay_after"
cmp -s "$replay_before" "$replay_after"

phase=postflight
capture_non_target_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo "non-target Memory V1 state changed" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity")" == "$((before_entity_count + 1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_apply")" == "$((before_apply_count + 1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_alias_observation")" == "$((before_alias_count + 1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.observation_entity_binding")" == "$((before_binding_count + 1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.relational_operation_request")" == "$((before_request_count + 1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity WHERE entity_id='$entity_id'::uuid AND owner_user_id='$owner'::uuid AND entity_type='concept' AND canonical_name='personal trainer' AND normalized_name='personal trainer' AND metadata->>'identity_state'='named' AND metadata->>'resolution_id'='$resolution'")" == "1" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_apply WHERE owner_user_id='$owner'::uuid AND resolution_id='$resolution'::uuid AND review_id='$review'::uuid AND applied_entity_id='$entity_id'::uuid AND apply_manifest_sha256='$manifest'")" == "1" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_alias_observation WHERE owner_user_id='$owner'::uuid AND resolution_id='$resolution'::uuid AND entity_id='$entity_id'::uuid AND alias_text='personal trainer'")" == "1" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.observation_entity_binding WHERE owner_user_id='$owner'::uuid AND observation_id='$observation'::uuid AND subject_entity_id='$self_entity'::uuid AND object_entity_id='$entity_id'::uuid")" == "1" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$owner'::uuid AND request_id='$request'::uuid AND operation='apply_resolution' AND manifest_sha256='$manifest'")" == "1" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity WHERE owner_user_id='$other_owner'::uuid AND metadata->>'resolution_id'='$resolution'")" == "0" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_apply WHERE owner_user_id='$other_owner'::uuid AND resolution_id='$resolution'::uuid")" == "0" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo "Qdrant changed during trainer resolution apply" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_trainer_resolution_apply_${run_id}.json"
AUTHORIZATION="$authorization" BACKUP="$backup" CATALOG="$catalog" \
  BASELINE="$baseline" POST="$post" REPLAY_BEFORE="$replay_before" \
  REPLAY_AFTER="$replay_after" REPORT="$report" ENTITY_ID="$entity_id" \
  QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
  HEAD="$(git -C "$repo_root" rev-parse HEAD)" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

auth = json.loads(Path(os.environ["AUTHORIZATION"]).read_text())
value = {
    "contract_version": "memory_v1_v5_trainer_resolution_apply_report_v1",
    "authorization_id": auth["authorization_id"],
    "phase_authorization": auth["phase_authorization"],
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": auth["owner_user_id"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "created": {
        "entity": 1,
        "entity_resolution_apply": 1,
        "entity_alias_observation": 1,
        "observation_entity_binding": 1,
        "relational_operation_request": 1,
        "total": 5,
        "entity_id": os.environ["ENTITY_ID"],
    },
    "evidence": {
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "replay_before": os.environ["REPLAY_BEFORE"],
        "replay_after": os.environ["REPLAY_AFTER"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "trainer_resolution_applied": True,
        "trainer_entity_created": True,
        "trainer_alias_observed": True,
        "occupation_observation_bound": True,
        "zero_write_replay": True,
        "other_owner_state_unchanged": True,
        "qdrant_unchanged": True,
        "projection_invoked": False,
    },
    "hard_stop": "before_projection_staging_review_or_runtime_activation",
}
Path(os.environ["REPORT"]).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_v5_trainer_resolution_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\nentity_id=%s\n' "$report" "$backup" "$entity_id"
