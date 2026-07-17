#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Transactionally applies one auto-link resolution and
# records one manual approval. It does not apply the approved create-new
# resolution, create an entity, bind an observation, or invoke projection.

if [[ $# -ne 1 ]]; then
  echo "usage: $0 AUTHORIZATION.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_INITIAL_RESOLUTION_APPLY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_INITIAL_RESOLUTION_APPLY=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
authorization=$(realpath "$1")
required_ancestor=e14e89d143efadbc05f6aa9cb8c58ee2609c3854
test_sql=tests/memory_v1_v5_initial_resolution_apply.sql
expected_test_sha=0638f56d39b62a9fe194c8430b400e898b07063ae660cdeaa3d07515f1635735
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
self_resolution=fc1859aa-f280-4aa1-b75a-ee882e969c87
self_entity=35029129-27bd-457b-8cb5-82dd37ba32ba
self_request=31000000-0000-4000-8000-000000000001
self_manifest=5e9960a36d27a6b50b50ede71dd43f49fc02d45522189fa57d304e86b7a3f479
trainer_resolution=a8befb71-413b-49b0-a460-c683ea52038e
trainer_request=31000000-0000-4000-8000-000000000002
trainer_manifest=2b046df82c08e71863882705536763263d17c646aba7abe0c0dfa8556e1d0cfe
trainer_reason='source explicitly states personal trainer; approve governed concept creation'
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_initial_resolution_apply.lock
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
    "contract_version", "authorization_id", "authorized", "authorized_by",
    "authorized_at", "expires_at", "expected_head_commit", "target_server",
    "scope", "owner_user_id", "self_resolution_id", "self_request_id",
    "self_apply_manifest_sha256", "trainer_resolution_id",
    "trainer_request_id", "trainer_review_manifest_sha256",
    "trainer_review_decision", "trainer_review_reason", "expected_rows_created",
    "test_sha256",
}
if set(value) != expected:
    raise SystemExit("authorization keys do not exactly match the contract")
if value["contract_version"] != "memory_v1_v5_initial_resolution_apply_authorization_v1":
    raise SystemExit("authorization contract mismatch")
if value["authorized"] is not True or value["authorized_by"] != "Eric Lund":
    raise SystemExit("authorization identity or flag mismatch")
uuid.UUID(value["authorization_id"])
if value["expected_head_commit"] != os.environ["EXPECTED_HEAD"]:
    raise SystemExit("authorization commit mismatch")
if value["target_server"] != "seebx":
    raise SystemExit("authorization server mismatch")
if value["scope"] != "apply_self_resolution_and_approve_personal_trainer_review":
    raise SystemExit("authorization scope mismatch")
exact = {
    "owner_user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
    "self_resolution_id": "fc1859aa-f280-4aa1-b75a-ee882e969c87",
    "self_request_id": "31000000-0000-4000-8000-000000000001",
    "self_apply_manifest_sha256": "5e9960a36d27a6b50b50ede71dd43f49fc02d45522189fa57d304e86b7a3f479",
    "trainer_resolution_id": "a8befb71-413b-49b0-a460-c683ea52038e",
    "trainer_request_id": "31000000-0000-4000-8000-000000000002",
    "trainer_review_manifest_sha256": "2b046df82c08e71863882705536763263d17c646aba7abe0c0dfa8556e1d0cfe",
    "trainer_review_decision": "approved",
    "trainer_review_reason": "source explicitly states personal trainer; approve governed concept creation",
    "expected_rows_created": 4,
    "test_sha256": "0638f56d39b62a9fe194c8430b400e898b07063ae660cdeaa3d07515f1635735",
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
flock -n 9 || { echo "another initial resolution apply holds the lock" >&2; exit 1; }
umask 077
auth_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' "$authorization")
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_initial_resolution_apply_${run_id}.status"

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

capture_unchanged_state() {
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
        'entity_resolution_apply',
        'entity_resolution_review',
        'relational_operation_request'
      )
    ORDER BY table_name
  ")
  printf 'entity_resolution_apply_other\t%s\n' \
    "$(table_state entity_resolution_apply "resolution_id <> '$self_resolution'::uuid")" \
    >>"$output"
  printf 'entity_resolution_review_other\t%s\n' \
    "$(table_state entity_resolution_review "resolution_id <> '$trainer_resolution'::uuid")" \
    >>"$output"
  printf 'relational_operation_request_other\t%s\n' \
    "$(table_state relational_operation_request "request_id NOT IN ('$self_request'::uuid,'$trainer_request'::uuid)")" \
    >>"$output"
  chmod 0600 "$output"
}

capture_all_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    printf '%s\t%s\n' "$table" "$(table_state "$table")" >>"$output"
  done < <(psql_scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

current_preflight() {
  actor_scalar "
    SELECT 'self',resolution_id,action,decision_state,
           prospective_entity_id,entity_state_sha256,apply_manifest_sha256
      FROM memory.preflight_entity_resolution_apply_v5(
        '$self_resolution'::uuid,NULL
      );
    SELECT 'trainer',resolution_id,action,decision_state,
           mention_sha256,candidate_set_sha256,decision_sha256,
           authorization_manifest_sha256
      FROM memory.preflight_entity_resolution_review_v5(
        '$trainer_resolution'::uuid,
        'approved'::memory.entity_review_decision,
        '$trainer_reason'
      );
  "
}

phase=preflight
expected_preflight="self|$self_resolution|link_existing|auto_link_eligible|$self_entity|ae861134f3a8f1dac4e88dad9dfc941f74b9b79d3cdc077c125cc5ca9b018a43|$self_manifest
trainer|$trainer_resolution|create_new|manual_review_required|32954def97c707addafd874e7cac71fc292b2167b4d389c1b3a8412b14b7af0a|4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945|401c64d6e2e25c3aeef87b19b7df5dc3b84461955a06f3bce2d70607b0b034b0|$trainer_manifest"
[[ "$(current_preflight)" == "$expected_preflight" ]] || {
  echo "initial resolution preflight drifted" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_apply WHERE resolution_id='$self_resolution'::uuid")" == "0" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_review WHERE resolution_id='$trainer_resolution'::uuid")" == "0" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.relational_operation_request WHERE request_id IN ('$self_request'::uuid,'$trainer_request'::uuid)")" == "0" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.observation_entity_binding")" == "0" ]]

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_initial_resolution_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_initial_resolution_post_${run_id}.tsv"
capture_unchanged_state "$baseline"
qdrant_before=$(qdrant_signature)
before_apply_count=$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_apply")
before_review_count=$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_review")
before_request_count=$(psql_scalar "SELECT count(*) FROM memory.relational_operation_request")

phase=backup
backup_partial="$snapshot_dir/.memory_pre_initial_resolution_apply_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_initial_resolution_apply_${run_id}.dump"
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
  echo "initial resolution preflight changed during backup" >&2
  exit 1
}

phase=transactional_apply
apply_output=$(docker exec -i "$container" psql -X -q -A -t -F '|' \
  -v ON_ERROR_STOP=1 -U sage -d "$database" <<SQL
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$owner',true) \gset
SELECT * FROM memory.apply_entity_resolution_v5(
  '$self_request'::uuid,'$self_resolution'::uuid,NULL,'$self_manifest'
) \gset self_
SELECT 1 / ((:'self_outcome'='applied')::integer);
SELECT 1 / ((:'self_bindings_created'::integer=0)::integer);
SELECT 1 / ((:'self_applied_entity_id'='$self_entity')::integer);
SELECT * FROM memory.review_entity_resolution_v5(
  '$trainer_request'::uuid,'$trainer_resolution'::uuid,
  'approved'::memory.entity_review_decision,
  '$trainer_reason','$trainer_manifest'
) \gset trainer_
SELECT 1 / ((:'trainer_outcome'='applied')::integer);
SELECT :'self_applied_entity_id',:'self_outcome',:'self_bindings_created',
       :'trainer_review_id',:'trainer_outcome';
RESET SESSION AUTHORIZATION;
COMMIT;
SQL
)
apply_output=$(printf '%s\n' "$apply_output" | sed '/^1$/d;/^[[:space:]]*$/d')
IFS='|' read -r applied_entity apply_outcome bindings_created review_id review_outcome <<<"$apply_output"
[[ "$applied_entity" == "$self_entity" && "$apply_outcome" == "applied" ]]
[[ "$bindings_created" == "0" && "$review_outcome" == "applied" ]]
[[ "$review_id" =~ ^[0-9a-f-]{36}$ ]]

phase=zero_write_replay
replay_before="$snapshot_dir/memory_v1_initial_resolution_replay_before_${run_id}.tsv"
replay_after="$snapshot_dir/memory_v1_initial_resolution_replay_after_${run_id}.tsv"
capture_all_state "$replay_before"
replay_counts_before=$(psql_scalar "SELECT (SELECT count(*) FROM memory.entity_resolution_apply)::text || '|' || (SELECT count(*) FROM memory.entity_resolution_review)::text || '|' || (SELECT count(*) FROM memory.relational_operation_request)::text")
replay_output=$(docker exec -i "$container" psql -X -q -A -t -F '|' \
  -v ON_ERROR_STOP=1 -U sage -d "$database" <<SQL
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$owner',true) \gset
SELECT * FROM memory.apply_entity_resolution_v5(
  '$self_request'::uuid,'$self_resolution'::uuid,NULL,'$self_manifest'
) \gset self_
SELECT * FROM memory.review_entity_resolution_v5(
  '$trainer_request'::uuid,'$trainer_resolution'::uuid,
  'approved'::memory.entity_review_decision,
  '$trainer_reason','$trainer_manifest'
) \gset trainer_
SELECT 1 / ((:'self_outcome'='replayed')::integer);
SELECT 1 / ((:'self_bindings_created'::integer=0)::integer);
SELECT 1 / ((:'trainer_outcome'='replayed')::integer);
SELECT :'self_outcome',:'self_bindings_created',:'trainer_review_id',:'trainer_outcome';
RESET SESSION AUTHORIZATION;
COMMIT;
SQL
)
replay_output=$(printf '%s\n' "$replay_output" | sed '/^1$/d;/^[[:space:]]*$/d')
IFS='|' read -r self_replay replay_bindings replay_review trainer_replay <<<"$replay_output"
[[ "$self_replay" == "replayed" && "$replay_bindings" == "0" ]]
[[ "$replay_review" == "$review_id" && "$trainer_replay" == "replayed" ]]
capture_all_state "$replay_after"
cmp -s "$replay_before" "$replay_after"
[[ "$(psql_scalar "SELECT (SELECT count(*) FROM memory.entity_resolution_apply)::text || '|' || (SELECT count(*) FROM memory.entity_resolution_review)::text || '|' || (SELECT count(*) FROM memory.relational_operation_request)::text")" == "$replay_counts_before" ]]

phase=postflight
capture_unchanged_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo "non-target Memory V1 state changed" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_apply")" == "$((before_apply_count + 1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_review")" == "$((before_review_count + 1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.relational_operation_request")" == "$((before_request_count + 2))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_apply WHERE owner_user_id='$owner'::uuid AND resolution_id='$self_resolution'::uuid AND review_id IS NULL AND applied_entity_id='$self_entity'::uuid AND apply_manifest_sha256='$self_manifest'")" == "1" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_review WHERE review_id='$review_id'::uuid AND owner_user_id='$owner'::uuid AND resolution_id='$trainer_resolution'::uuid AND decision='approved' AND reviewer_type='user' AND reviewer_ref='$owner' AND reason='$trainer_reason' AND authorization_manifest_sha256='$trainer_manifest'")" == "1" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$owner'::uuid AND ((request_id='$self_request'::uuid AND operation='apply_resolution' AND manifest_sha256='$self_manifest') OR (request_id='$trainer_request'::uuid AND operation='review_resolution' AND manifest_sha256='$trainer_manifest'))")" == "2" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.observation_entity_binding")" == "0" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid AND entity_id='$self_entity'::uuid AND metadata->>'identity_state'='trusted_owner_self'")" == "1" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_apply WHERE owner_user_id='$other_owner'::uuid")" == "0" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.entity_resolution_review WHERE owner_user_id='$other_owner'::uuid")" == "0" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo "Qdrant changed during initial resolution apply" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_initial_resolution_apply_${run_id}.json"
AUTHORIZATION="$authorization" BACKUP="$backup" CATALOG="$catalog" \
  BASELINE="$baseline" POST="$post" REPLAY_BEFORE="$replay_before" \
  REPLAY_AFTER="$replay_after" REPORT="$report" REVIEW_ID="$review_id" \
  QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
  HEAD="$(git -C "$repo_root" rev-parse HEAD)" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

auth = json.loads(Path(os.environ["AUTHORIZATION"]).read_text())
value = {
    "contract_version": "memory_v1_v5_initial_resolution_apply_report_v1",
    "authorization_id": auth["authorization_id"],
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": auth["owner_user_id"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "created": {
        "entity_resolution_apply": 1,
        "entity_resolution_review": 1,
        "relational_operation_request": 2,
        "total": 4,
        "trainer_review_id": os.environ["REVIEW_ID"],
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
        "self_resolution_applied": True,
        "trainer_resolution_review_approved": True,
        "trainer_resolution_not_applied": True,
        "entity_rows_unchanged": True,
        "observation_bindings_created": 0,
        "zero_write_replay": True,
        "other_owner_state_unchanged": True,
        "qdrant_unchanged": True,
        "projection_invoked": False,
    },
    "hard_stop": "before_personal_trainer_entity_resolution_apply_or_projection",
}
Path(os.environ["REPORT"]).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_v5_initial_resolution_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\nreview_id=%s\n' "$report" "$backup" "$review_id"
